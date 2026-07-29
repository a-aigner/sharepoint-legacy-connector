"""HTTP transport: session, auth, legacy TLS, rate limiting, retries, version probe.

NTLM is a *connection-oriented* scheme: it authenticates the TCP connection, not
the request. Everything therefore goes through one pooled ``requests.Session``
with keep-alive left on. Disabling pooling here would make every call
re-handshake, which on a farm of this vintage is the difference between a crawl
that finishes overnight and one that does not finish.
"""

from __future__ import annotations

import importlib
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

from .config import Settings, get_logger, register_secret
from .trace import BodyTrace

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

REDACTED_HEADER = "***REDACTED***"

#: Headers safe to log verbatim. This is an **allowlist**: anything not named
#: here is redacted. A denylist cannot cover a header nobody thought of — a
#: reverse proxy's own ``X-Forwarded-Authorization``, a vendor token — and the
#: failure mode of guessing wrong is a credential sitting in a log file.
LOGGABLE_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "cache-control",
        "content-encoding",
        "content-length",
        "content-type",
        "date",
        "etag",
        "expires",
        "last-modified",
        "location",
        "microsoftsharepointteamservices",
        "server",
        "soapaction",
        "transfer-encoding",
        "user-agent",
        "x-powered-by",
    }
)

#: Known-sensitive names, kept for reporting. The allowlist above is the control.
SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


def redact_headers(headers: Any) -> dict[str, str]:
    """Redact every header not explicitly known to be safe.

    ``WWW-Authenticate`` is special-cased down to its scheme names: the value
    can carry a Negotiate/GSSAPI token, but the schemes are the diagnostic part
    and the reason the auth probe exists.
    """
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        lowered = key.lower()
        if lowered in LOGGABLE_HEADERS:
            out[key] = value
        elif lowered == "www-authenticate":
            out[key] = ", ".join(_parse_auth_schemes(str(value))) or REDACTED_HEADER
        else:
            out[key] = REDACTED_HEADER
    return out


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


#: Providers tried, in order, for ``SP_AUTH_MODE=integrated``. Each is optional
#: and platform-specific, so they are imported lazily and a clear install hint is
#: raised if none is present.
INTEGRATED_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    ("requests_negotiate_sspi", "HttpNegotiateAuth", "spconnect[windows]"),
    ("requests_gssapi", "HTTPSPNEGOAuth", "spconnect[kerberos]"),
    ("requests_kerberos", "HTTPKerberosAuth", "spconnect[kerberos]"),
)


class TransportError(Exception):
    """Base class for transport-level failures."""


class IntegratedAuthUnavailable(TransportError):
    """``SP_AUTH_MODE=integrated`` was requested but no provider is installed."""


def build_integrated_auth() -> Any:
    """Authenticate as the *current process identity*. No password anywhere.

    Windows SSPI first (the common case on a domain-joined box), then Kerberos
    via an existing ticket. Returns a ``requests`` auth object.
    """
    attempted: list[str] = []
    for module_name, class_name, extra in INTEGRATED_PROVIDERS:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            attempted.append(f"{module_name} (install: pip install '{extra}')")
            continue
        factory = getattr(module, class_name, None)
        if factory is None:  # pragma: no cover - provider API drift
            attempted.append(f"{module_name}.{class_name} missing")
            continue
        log.info("auth.integrated", provider=f"{module_name}.{class_name}")
        return factory()

    raise IntegratedAuthUnavailable(
        "SP_AUTH_MODE=integrated needs a platform auth provider, none of which is installed:\n  "
        + "\n  ".join(attempted)
        + "\nOn a domain-joined Windows box: pip install 'spconnect[windows]', then run the "
        "crawl as the service account. With a Kerberos ticket: pip install 'spconnect[kerberos]' "
        "and kinit first."
    )


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

    @property
    def supports_all_sub_web_collection(self) -> bool:
        """``Webs.GetAllSubWebCollection`` also arrived with WSS 3.0.

        Unknown versions get the benefit of the doubt: try the fast path, and
        fall back if the server disagrees.
        """
        return self.major is None or self.major >= 12

    @property
    def has_list_view_threshold(self) -> bool:
        """The 5000-item list view threshold arrived with SharePoint 2010 (major 14).

        WSS 3.0 had no such limit: large lists were slow but never blocked.
        """
        return self.major is not None and self.major >= 14

    def as_dict(self) -> dict[str, Any]:
        return {
            "raw": self.raw,
            "major": self.major,
            "has_list_view_threshold": self.has_list_view_threshold,
            "product": self.product,
            "supports_change_tokens": self.supports_change_tokens,
            "supports_all_sub_web_collection": self.supports_all_sub_web_collection,
        }


@dataclass(frozen=True)
class AuthProbe:
    """What the server offers an *unauthenticated* request.

    "We only need a username and password" describes NTLM and Basic equally
    well — both take exactly that, they just transmit it differently. The only
    reliable discriminator is the ``WWW-Authenticate`` header on a 401, so ask
    the server rather than guess.
    """

    status: int | None
    schemes: list[str]
    forms_login_url: str | None = None
    error: str | None = None

    @property
    def suggested_mode(self) -> str | None:
        """The ``SP_AUTH_MODE`` this server appears to want, or ``None``."""
        lowered = {s.lower() for s in self.schemes}
        if "ntlm" in lowered or "negotiate" in lowered:
            return "ntlm"
        if "basic" in lowered:
            return "basic"
        if self.forms_login_url:
            return None  # forms-based auth is out of scope
        if self.status is not None and self.status < 400:
            return "anonymous"
        return None

    @property
    def advice(self) -> str:
        if self.error:
            return f"could not determine ({self.error})"
        if self.forms_login_url:
            return (
                f"forms-based auth (redirects to {self.forms_login_url}). "
                "NOT SUPPORTED by this connector — that is a follow-up."
            )
        if not self.schemes:
            if self.status is not None and self.status < 400:
                return "server answered without a challenge; SP_AUTH_MODE=anonymous may work"
            return "server sent no WWW-Authenticate header"
        offered = ", ".join(self.schemes)
        mode = self.suggested_mode
        note = ""
        if {s.lower() for s in self.schemes} == {"negotiate"}:
            note = " (Kerberos only — NTLM may be refused; report this)"
        return f"{offered} -> SP_AUTH_MODE={mode}{note}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "schemes": self.schemes,
            "forms_login_url": self.forms_login_url,
            "suggested_mode": self.suggested_mode,
            "error": self.error,
        }


def _parse_auth_schemes(header: str) -> list[str]:
    """``'Negotiate, NTLM, Basic realm="x"'`` -> ``['Negotiate', 'NTLM', 'Basic']``."""
    schemes: list[str] = []
    for part in header.split(","):
        token = part.strip().split(" ", 1)[0].strip()
        if token and token.lower() not in {s.lower() for s in schemes}:
            schemes.append(token)
    return schemes


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
        if settings.needs_password:
            # Registered before any request, so the scrubber can strip it from
            # every log line. In `integrated` mode there is nothing to register.
            register_secret(settings.password.get_secret_value(), username=settings.username)
        self.limiter = RateLimiter(settings.requests_per_second)
        self.session = self._build_session(settings)
        self.server_version: ServerVersion | None = None
        self.request_count = 0
        self.bytes_received = 0
        self.trace = (
            BodyTrace(settings.resolved_trace_file, settings.log_body_chars) if settings.log_bodies else None
        )

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

        if settings.auth_mode == "integrated":
            # No credential material enters this process at all.
            session.auth = build_integrated_auth()
        elif settings.auth_mode == "ntlm":
            from requests_ntlm import HttpNtlmAuth

            session.auth = HttpNtlmAuth(settings.username, settings.password.get_secret_value())
        elif settings.auth_mode == "basic":
            log.warning(
                "auth.basic",
                detail="Basic sends the password on every request. Prefer SP_AUTH_MODE=integrated, "
                "or ntlm, which never transmits it.",
            )
            session.auth = HTTPBasicAuth(settings.username, settings.password.get_secret_value())
        else:
            session.auth = None
        return session

    def close(self) -> None:
        if self.trace is not None:
            self.trace.close()
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
        self.request_count += 1
        sequence = self.request_count
        body = kwargs.get("data")
        log.debug(
            "http.request",
            seq=sequence,
            method=method,
            url=url,
            request_bytes=len(body) if isinstance(body, bytes | str) else None,
            headers=redact_headers(kwargs.get("headers") or {}),
        )
        if self.trace is not None and isinstance(body, bytes | str):
            self.trace.write(sequence, "REQUEST", f"{method} {url}", body)

        waited = time.monotonic()
        self.limiter.acquire()
        throttled = time.monotonic() - waited
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
        size = None if stream else len(response.content)
        if size:
            self.bytes_received += size
        log.debug(
            "http.response",
            seq=sequence,
            method=method,
            url=url,
            status=response.status_code,
            duration=duration,
            rate_limit_wait=round(throttled, 3) if throttled > 0.001 else None,
            response_bytes=size,
            content_type=response.headers.get("Content-Type"),
        )
        if self.trace is not None and not stream:
            self.trace.write(sequence, "RESPONSE", f"HTTP {response.status_code} {url}", response.content)

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

    # ---- auth probe ----

    def probe_auth_schemes(self, url: str | None = None) -> AuthProbe:
        """Ask the server which HTTP auth schemes it offers, sending no credentials.

        Deliberately bypasses the retry/401 machinery: here a 401 is the answer,
        not a failure.
        """
        target = url or self.settings.base_url
        saved_auth = self.session.auth
        self.session.auth = None
        try:
            self.limiter.acquire()
            response = self.session.get(target, timeout=self.settings.timeout_seconds, allow_redirects=False)
        except (requests.ConnectionError, requests.Timeout) as exc:
            return AuthProbe(status=None, schemes=[], error=f"{type(exc).__name__}: {exc}")
        finally:
            self.session.auth = saved_auth

        schemes = _parse_auth_schemes(response.headers.get("WWW-Authenticate", ""))

        forms_login: str | None = None
        location = response.headers.get("Location", "")
        if response.is_redirect and "login.aspx" in location.lower():
            forms_login = location
        elif response.status_code == 200 and b"login.aspx" in response.content[:8192].lower():
            forms_login = target

        probe = AuthProbe(status=response.status_code, schemes=schemes, forms_login_url=forms_login)
        log.info("auth_probe", status=probe.status, schemes=schemes, suggested=probe.suggested_mode)
        return probe

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
