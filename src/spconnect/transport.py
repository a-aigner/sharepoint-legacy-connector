"""HTTP transport: session, auth, legacy TLS, rate limiting, retries, version probe.

NTLM is a *connection-oriented* scheme: it authenticates the TCP connection, not
the request. Everything therefore goes through one pooled ``requests.Session``
with keep-alive left on. Disabling pooling here would make every call
re-handshake, which on a farm of this vintage is the difference between a crawl
that finishes overnight and one that does not finish.
"""

from __future__ import annotations

import re
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings, get_logger

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})
VERSION_HEADER = "MicrosoftSharePointTeamServices"

#: Major build number -> human product name.
SHAREPOINT_PRODUCTS = {
    6: "WSS 2.0 / SharePoint Portal Server 2003",
    11: "SharePoint Portal Server 2003 (v11)",
    12: "WSS 3.0 / MOSS 2007",
    14: "SharePoint 2010",
    15: "SharePoint 2013",
    16: "SharePoint 2016/2019/SE",
}


class TransportError(Exception):
    """Base class for transport-level failures."""


class AuthenticationError(TransportError):
    """401/403. Never retried — a bad credential must fail fast and loudly."""


class NotFoundError(TransportError):
    """404. Never retried."""


class HttpError(TransportError):
    """Non-retryable, non-auth HTTP error status."""

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        super().__init__(f"HTTP {status_code} for {url}: {body[:500]}")
        self.status_code = status_code
        self.url = url
        self.body = body


class RetryableTransportError(TransportError):
    """Signals the tenacity retry loop that another attempt is worthwhile."""


@dataclass(frozen=True)
class ServerVersion:
    """Result of the ``MicrosoftSharePointTeamServices`` header probe."""

    raw: str | None
    major: int | None

    @property
    def product(self) -> str:
        if self.major is None:
            return "unknown"
        return SHAREPOINT_PRODUCTS.get(self.major, f"unknown build major {self.major}")

    @property
    def supports_change_tokens(self) -> bool:
        """``GetListItemChangesSinceToken`` arrived with WSS 3.0 (major 12)."""
        return self.major is not None and self.major >= 12

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "major": self.major,
            "product": self.product,
            "supports_change_tokens": self.supports_change_tokens,
        }


class RateLimiter:
    """Sleep-based limiter. Thread-safe; shared by every outbound request."""

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class LegacyTLSAdapter(HTTPAdapter):
    """Adapter that will talk TLS 1.0 with ciphers modern OpenSSL refuses by default."""

    def __init__(self, verify_ssl: bool, **kwargs: Any) -> None:
        self._verify_ssl = verify_ssl
        super().__init__(**kwargs)

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers("DEFAULT@SECLEVEL=0")
        except ssl.SSLError:  # pragma: no cover - depends on the local OpenSSL build
            log.warning("legacy_tls.seclevel_unsupported", detail="could not lower cipher security level")
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1
        except (ValueError, OSError):  # pragma: no cover - build-dependent
            log.warning("legacy_tls.min_version_unsupported", detail="OpenSSL refuses TLS 1.0")
        if not self._verify_ssl:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def init_poolmanager(self, *args: Any, **kwargs: Any) -> None:
        kwargs["ssl_context"] = self._context()
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args: Any, **kwargs: Any) -> Any:
        kwargs["ssl_context"] = self._context()
        return super().proxy_manager_for(*args, **kwargs)


#: Matches ``<soap:Fault``, ``<SOAP-ENV:Fault``, ``<Fault`` or ``<faultstring``.
#: A bare substring search for "fault" is not enough — ``DefaultViewUrl`` is an
#: attribute on every single ``<List>`` element this farm returns.
_FAULT_RE = re.compile(rb"<\s*(?:[a-z0-9_.-]+:)?fault(?:string)?[\s>/]", re.IGNORECASE)


def _looks_like_soap_fault(body: bytes) -> bool:
    """A 500 carrying a SOAP fault is an application error, not a flaky server."""
    return _FAULT_RE.search(body[:8192]) is not None


class Transport:
    """Owns the session. One instance per process; safe to share across threads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limiter = RateLimiter(settings.requests_per_second)
        self.session = self._build_session(settings)
        self.server_version: ServerVersion | None = None

    # ---- session construction ----

    @staticmethod
    def _build_session(settings: Settings) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": "spconnect/0.1 (legacy SharePoint extraction; read-only)",
                "Accept-Encoding": "gzip, deflate",
            }
        )
        session.verify = settings.verify_ssl

        pool = max(settings.concurrency * 2, 4)
        if settings.allow_legacy_tls:
            log.warning(
                "legacy_tls.enabled",
                detail="TLS 1.0 and SECLEVEL=0 ciphers permitted; certificate checking "
                f"{'off' if not settings.verify_ssl else 'on'}",
            )
            adapter: HTTPAdapter = LegacyTLSAdapter(
                verify_ssl=settings.verify_ssl, pool_connections=pool, pool_maxsize=pool
            )
        else:
            adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
        session.mount("https://", adapter)
        session.mount("http://", HTTPAdapter(pool_connections=pool, pool_maxsize=pool))

        if settings.auth_mode == "ntlm":
            from requests_ntlm import HttpNtlmAuth

            session.auth = HttpNtlmAuth(settings.username, settings.password.get_secret_value())
        elif settings.auth_mode == "basic":
            session.auth = HTTPBasicAuth(settings.username, settings.password.get_secret_value())
        else:
            session.auth = None
        return session

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- core request path ----

    def _retrying(self) -> Retrying:
        def _before_sleep(state: RetryCallState) -> None:
            log.warning(
                "http.retry",
                attempt=state.attempt_number,
                sleep=round(getattr(state.next_action, "sleep", 0.0), 2),
                error=str(state.outcome.exception()) if state.outcome else None,
            )

        return Retrying(
            stop=stop_after_attempt(max(1, self.settings.max_retries)),
            wait=wait_exponential(multiplier=self.settings.backoff_base_seconds, max=120),
            retry=retry_if_exception_type(RetryableTransportError),
            before_sleep=_before_sleep,
            reraise=True,
        )

    def _send(self, method: str, url: str, *, stream: bool = False, **kwargs: Any) -> requests.Response:
        self.limiter.acquire()
        started = time.monotonic()
        try:
            response = self.session.request(
                method,
                url,
                timeout=self.settings.timeout_seconds,
                stream=stream,
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableTransportError(f"{type(exc).__name__} for {url}: {exc}") from exc

        duration = round(time.monotonic() - started, 3)
        log.debug("http.response", method=method, url=url, status=response.status_code, duration=duration)

        if response.status_code in (401, 403):
            raise AuthenticationError(
                f"HTTP {response.status_code} for {url}. Check SP_USERNAME / SP_PASSWORD / SP_AUTH_MODE "
                "and that the account may read this web."
            )
        if response.status_code == 404:
            raise NotFoundError(f"HTTP 404 for {url}")
        if response.status_code in RETRYABLE_STATUS:
            # A 500 carrying a SOAP fault is the application talking; hand it back
            # to the SOAP layer instead of hammering the server five more times.
            if response.status_code == 500 and not stream and _looks_like_soap_fault(response.content):
                return response
            raise RetryableTransportError(f"HTTP {response.status_code} for {url}")
        return response

    def request(self, method: str, url: str, *, stream: bool = False, **kwargs: Any) -> requests.Response:
        """Send with the configured retry policy. Auth/404 failures are not retried."""
        return self._retrying()(self._send, method, url, stream=stream, **kwargs)

    # ---- convenience wrappers ----

    def post_soap(self, endpoint: str, body: bytes, soap_action: str) -> bytes:
        """POST a SOAP envelope. Returns raw bytes; fault parsing lives in :mod:`soap`."""
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{soap_action}"',
        }
        response = self.request("POST", endpoint, data=body, headers=headers)
        return response.content

    def get(self, url: str, *, stream: bool = False) -> requests.Response:
        response = self.request("GET", url, stream=stream)
        if response.status_code >= 400:
            raise HttpError(response.status_code, url, "" if stream else response.text)
        return response

    def stream_get(self, url: str) -> Iterator[requests.Response]:
        """Context-manager-ish helper kept explicit for the file downloader."""
        response = self.get(url, stream=True)
        try:
            yield response
        finally:
            response.close()

    # ---- version probe ----

    def probe_version(self, url: str | None = None) -> ServerVersion:
        """Read ``MicrosoftSharePointTeamServices`` from any page on the farm."""
        target = url or self.settings.base_url or "/"
        try:
            response = self.request("HEAD", target, allow_redirects=True)
            if response.status_code >= 400 or VERSION_HEADER not in response.headers:
                response = self.request("GET", target, allow_redirects=True)
        except NotFoundError:
            response = self.request("GET", self.settings.base_url + "/_vti_bin/Lists.asmx")

        raw = response.headers.get(VERSION_HEADER)
        major = None
        if raw:
            match = re.match(r"\s*(\d+)", raw)
            if match:
                major = int(match.group(1))

        version = ServerVersion(raw=raw, major=major)
        self.server_version = version

        if raw is None:
            log.warning(
                "version_probe.missing_header",
                detail=f"no {VERSION_HEADER} header; server version unknown, assuming WSS 3.0 semantics",
                url=target,
            )
        else:
            log.info("version_probe", raw=raw, major=major, product=version.product)

        if major is not None and major < 12:
            log.warning(
                "version_probe.pre_wss3",
                detail=(
                    f"Server reports major version {major} ({version.product}). "
                    "GetListItemChangesSinceToken and some query options may be unavailable; "
                    "`spconnect sync` will fall back to full crawls."
                ),
            )
        return version
