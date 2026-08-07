"""HTTP transport: session, auth, legacy TLS, rate limiting, retries, version probe.

NTLM is a *connection-oriented* scheme: it authenticates the TCP connection, not
the request. Everything therefore goes through one pooled ``requests.Session``
with keep-alive left on. Disabling pooling here would make every call
re-handshake, which on a farm of this vintage is the difference between a crawl
that finishes overnight and one that does not finish.
"""

from __future__ import annotations

import base64
import binascii
import importlib
import re
import ssl
import struct
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from tenacity import RetryCallState, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import Settings, get_logger, register_secret
from .trace import BodyTrace

log = get_logger(__name__)

RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

#: Every status that moves a request elsewhere. SOAP POSTs refuse all of them,
#: including the body-preserving 307/308 — a SOAP endpoint that is not where we
#: were told it is means the base URL is wrong, and saying so beats guessing.
REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

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
#: ``(module, class, how to get it)``. The third field is prose rather than an
#: extra name because they do not map one-to-one: ``spconnect[kerberos]``
#: installs ``requests-gssapi``, and ``requests-kerberos`` is only honoured if
#: something else already brought it in.
INTEGRATED_PROVIDERS: tuple[tuple[str, str, str], ...] = (
    (
        "requests_negotiate_sspi",
        "HttpNegotiateAuth",
        "Windows only — pip install 'spconnect[windows]'",
    ),
    ("requests_gssapi", "HTTPSPNEGOAuth", "Kerberos — pip install 'spconnect[kerberos]'"),
    ("requests_kerberos", "HTTPKerberosAuth", "Kerberos, older binding — not installed by any extra"),
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
            attempted.append(f"{module_name} — {extra}")
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
        + "\n\nOn domain-joined Windows: pip install 'spconnect[windows]', then run as the"
        "\nservice account. Elsewhere: pip install 'spconnect[kerberos]' and kinit first."
        "\n\nBefore installing anything, check what the farm actually offers — step 2 of"
        "\n`spconnect probe` prints it. The Kerberos provider speaks Negotiate/SPNEGO, so"
        "\na farm whose challenge is NTLM only cannot use it however it is installed, and"
        "\nintegrated auth is then unavailable on this platform. Stay on SP_AUTH_MODE=ntlm"
        "\nthere; it is not the weaker option against such a farm, it is the only one."
    )


class AuthenticationError(TransportError):
    """401/403. Never retried — a bad credential must fail fast and loudly."""


#: Pages SharePoint sends an *authenticated but unauthorised* browser request
#: to. They answer **HTTP 200** and carry the normal version header, so a status
#: check reads them as success — which is how an account with no permissions at
#: all can sail through a login step and fail three steps later on the first
#: request that has no friendly page to redirect to.
DENIAL_PAGES = ("accessdenied.aspx", "login.aspx", "signin.aspx", "authenticate.aspx")


def denial_page(*urls: str) -> str | None:
    """The first URL that is a sign-in or access-denied page, if any."""
    for url in urls:
        lowered = (url or "").lower()
        if any(marker in lowered for marker in DENIAL_PAGES):
            return url
    return None


class SharePointAccessDenied(AuthenticationError):
    """IIS accepted the credential; SharePoint then refused it access.

    Two different systems say no in two different ways here. IIS decides
    *whether you are who you say you are* and answers 401 when it doubts you.
    SharePoint decides *whether that identity may read this* and, for a browser
    request, answers by redirecting to an access-denied page with status 200.

    Subclasses :class:`AuthenticationError` on purpose. It is an authorisation
    failure rather than an authentication one, but every caller that already
    handles a refused login — the exit code, the shared diagnostic report, the
    re-raise in the web walk — should treat it identically, and a parallel
    hierarchy would mean each of them growing a second ``except`` that someone
    eventually forgets.
    """


NTLM_SIGNATURE = b"NTLMSSP\x00"

#: A minimal NTLM *Type 1* (Negotiate) message, constant because it carries no
#: identity: no username, no password, no workstation. Its only purpose is to
#: make the server answer with a Type 2 challenge — and a Type 2 names the
#: domain the server belongs to. Flags request Unicode, OEM, a target name, NTLM
#: and extended session security (``0x00088207``), which is what every client
#: sends and what every server of this era expects.
NTLM_NEGOTIATE = base64.b64encode(NTLM_SIGNATURE + struct.pack("<II", 1, 0x00088207) + b"\x00" * 16).decode()

#: AV-pair identifiers inside a Type 2 ``TargetInfo`` block (MS-NLMP 2.2.2.1).
_AV_NB_COMPUTER, _AV_NB_DOMAIN, _AV_DNS_COMPUTER, _AV_DNS_DOMAIN = 1, 2, 3, 4


@dataclass(frozen=True)
class NtlmTarget:
    """What the server volunteered about itself in an NTLM challenge."""

    netbios_domain: str | None = None
    dns_domain: str | None = None
    netbios_computer: str | None = None
    dns_computer: str | None = None

    @property
    def username_hint(self) -> str | None:
        """The ``SP_USERNAME`` form this server is asking for."""
        if self.netbios_domain:
            return f"{self.netbios_domain}\\<user>"
        if self.dns_domain:
            return f"<user>@{self.dns_domain}"
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "netbios_domain": self.netbios_domain,
            "dns_domain": self.dns_domain,
            "netbios_computer": self.netbios_computer,
            "dns_computer": self.dns_computer,
        }


def parse_ntlm_challenge(blob: bytes) -> NtlmTarget | None:
    """Read the domain out of an NTLM Type 2 challenge.

    Answers "what is our domain?" from the server itself, which matters because
    the people who need ``DOMAIN\\user`` are routinely the ones who do not know
    what ``DOMAIN`` is — and the server says so, unauthenticated, on any 401.

    Hand-parsed rather than pulled from ``spnego``: this has to work in
    ``basic`` and ``anonymous`` modes, where the NTLM stack is never loaded.
    Every offset is bounds-checked because this is untrusted network input;
    anything malformed yields ``None`` rather than an exception.
    """
    if len(blob) < 48 or not blob.startswith(NTLM_SIGNATURE):
        return None
    if struct.unpack_from("<I", blob, 8)[0] != 2:  # not a Challenge message
        return None

    flags = struct.unpack_from("<I", blob, 20)[0]
    unicode_encoding = "utf-16-le" if flags & 0x1 else "latin-1"

    def text_at(offset: int, length: int, encoding: str) -> str | None:
        if length <= 0 or offset + length > len(blob):
            return None
        try:
            return blob[offset : offset + length].decode(encoding).strip() or None
        except (UnicodeDecodeError, LookupError):
            return None

    name_len, _, name_offset = struct.unpack_from("<HHI", blob, 12)
    target_name = text_at(name_offset, name_len, unicode_encoding)

    info_len, _, info_offset = struct.unpack_from("<HHI", blob, 40)
    found: dict[int, str] = {}
    cursor, end = info_offset, min(info_offset + info_len, len(blob))
    while cursor + 4 <= end:
        av_id, av_len = struct.unpack_from("<HH", blob, cursor)
        cursor += 4
        if av_id == 0 or cursor + av_len > end:  # MsvAvEOL, or a truncated pair
            break
        value = text_at(cursor, av_len, "utf-16-le")
        if value:
            found[av_id] = value
        cursor += av_len

    target = NtlmTarget(
        # TargetName is the NetBIOS domain on a domain-joined server; the AV
        # pair is authoritative when both are present.
        netbios_domain=found.get(_AV_NB_DOMAIN) or target_name,
        dns_domain=found.get(_AV_DNS_DOMAIN),
        netbios_computer=found.get(_AV_NB_COMPUTER),
        dns_computer=found.get(_AV_DNS_COMPUTER),
    )
    return target if any(target.as_dict().values()) else None


def describe_auth_failure(response: requests.Response, *, auth_mode: str, username: str) -> str:
    """Everything a 401/403 can be made to say, in one block.

    "Check SP_USERNAME / SP_PASSWORD" is true of every possible cause and
    therefore useless. These four facts separate the causes that actually
    differ, and all of them are already on the response:

    * **Did we send a credential at all?** No ``Authorization`` header means the
      auth handler never ran — a configuration problem, not a rejection.
    * **Did the server re-challenge?** A 401 *with* ``WWW-Authenticate`` means
      the credential was rejected. A 401 *without* one generally means it was
      accepted and then denied access — an authorisation problem, so no amount
      of password-fixing will help.
    * **How many round trips?** NTLM needs three. One means the handshake never
      got started; two means it broke halfway.
    * **Did the server close the connection?** NTLM authenticates a *connection*,
      so a close mid-handshake fails it regardless of the credential.
    """
    request_headers = getattr(response.request, "headers", {}) or {}
    sent = "authorization" in {k.lower() for k in request_headers}
    challenge = response.headers.get("WWW-Authenticate", "")
    schemes = _parse_auth_schemes(challenge)
    legs = len(response.history) + 1

    lines = [f"HTTP {response.status_code} for {response.url}"]

    if not sent:
        lines.append(
            f"  credential  : NONE SENT — no Authorization header left this process. "
            f"SP_AUTH_MODE={auth_mode} did not attach one."
        )
    else:
        lines.append(f"  credential  : sent ({auth_mode}) over {legs} round trip(s)")

    if schemes:
        offered = {s.lower() for s in schemes}
        # "Basic" and "ntlm" name both a setting and a wire scheme; when the two
        # disagree the credential was never in the running, and blaming the
        # password sends the operator to reset an account that is fine.
        # What each mode can actually put on the wire. `integrated` is not a
        # scheme of its own: SSPI and SPNEGO negotiate NTLM or Kerberos, so it
        # counts as unoffered only when the server offers neither.
        usable = {
            "ntlm": {"ntlm", "negotiate"},
            "basic": {"basic"},
            "integrated": {"ntlm", "negotiate"},
        }.get(auth_mode, set())
        if usable and not (usable & offered):
            lines.append(
                f"  server says : it does not offer {auth_mode} at all — only "
                f"{', '.join(schemes)}. The credential was never tried."
            )
            lines.append(f"  check       : set SP_AUTH_MODE to match, e.g. {sorted(offered)[0]}")
            if body := (response.text or "").strip():
                lines.append(f"  body        : {body[:300]}")
            return "\n".join(lines)

        lines.append(
            f"  server says : rejected it, and re-challenges with {', '.join(schemes)} "
            "— the credential itself was refused"
        )
        if auth_mode == "ntlm" and username and "\\" not in username and "@" not in username:
            lines.append(
                f"  username    : '{username}' has no domain part. NTLM usually needs "
                "DOMAIN\\user (NetBIOS) or user@domain.tld."
            )
        lines.append("  check       : SP_USERNAME / SP_PASSWORD / SP_AUTH_MODE")
    elif sent:
        lines.append(
            "  server says : no WWW-Authenticate challenge on the rejection — the login "
            "was likely ACCEPTED and then denied access. This reads as a permissions "
            "problem on this resource, not a wrong password."
        )
        lines.append("  check       : the account's permissions on this web, before SP_USERNAME")
    elif auth_mode in ("ntlm", "integrated"):
        # NTLM and Negotiate are challenge-response: the client sends nothing
        # until the server asks. A 401 that carries no challenge therefore ends
        # the exchange before it begins — the handshake is never started, and
        # the server's own logs show the request arriving with no credentials.
        # It looks like the client failing to authenticate; it is the server
        # never inviting it to.
        lines.append(
            "  server says : refused WITHOUT a WWW-Authenticate challenge, so the handshake "
            "never began. NTLM sends nothing until it is asked, and it was not asked — which "
            "is why the server logs show this request arriving with no credentials."
        )
        lines.append(
            "  check       : whether a bodyless GET to this same URL is challenged normally. "
            "If it is, the endpoint only withholds the challenge from requests carrying a "
            "body, and SP_NTLM_PRIME_CONNECTION is the workaround."
        )
    else:
        lines.append("  server says : no WWW-Authenticate challenge")
        lines.append(f"  check       : SP_AUTH_MODE is '{auth_mode}' — the server wanted a credential")

    if response.headers.get("Connection", "").lower() == "close":
        lines.append(
            "  connection  : server sent 'Connection: close'. NTLM authenticates the "
            "connection, so a close mid-handshake fails it whatever the credential is."
        )

    body = (response.text or "").strip()
    if body:
        lines.append(f"  body        : {body[:300]}")

    return "\n".join(lines)


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


def redirect_target(source: str, location: str) -> str | None:
    """The ``SP_BASE_URL`` a redirect implies, or ``None`` if it implies none.

    Scheme and host come from ``location``; the **path comes from ``source``**.
    Farms rewrite the path while redirecting — the one that prompted this turns
    ``/_vti_bin/Webs.asmx`` into ``/Webs.asmx`` — so the target's path says
    nothing about where the site collection lives. Taking it would move a
    ``/sites/service`` base URL to the web application root and send the
    operator somewhere no less wrong than where they started.
    """
    src, dst = urlsplit(source), urlsplit(location)
    if not dst.scheme and not dst.netloc:
        return None  # relative redirect: same origin, so the base URL is fine
    scheme = dst.scheme or src.scheme
    netloc = dst.netloc or src.netloc
    if (scheme, netloc) == (src.scheme, src.netloc):
        return None
    return urlunsplit((scheme, netloc, src.path, "", "")).rstrip("/")


def redirect_advice(source: str, location: str) -> str:
    """One operator-facing explanation of a redirect, shared by every layer.

    Deliberately single-sourced: the transport meets redirects at the auth
    probe, at SOAP POSTs and at the base-URL check, and three hand-written
    variants of this paragraph would drift into three different diagnoses of
    the same fact.
    """
    if "login.aspx" in location.lower():
        return (
            "The target is a forms-authentication login page, which this connector "
            "does not support. The farm must offer NTLM, Negotiate or Basic."
        )

    src, dst = urlsplit(source), urlsplit(location)
    target = redirect_target(source, location)
    if target is None:
        return (
            "The redirect stays on this origin, so SP_BASE_URL is probably right; "
            f"the path may not be: {location}"
        )
    if dst.scheme and dst.scheme != src.scheme:
        return (
            f"The server redirects {src.scheme} to {dst.scheme}. Set SP_BASE_URL to "
            f"{target} so requests go directly to the scheme the farm actually serves."
        )
    return (
        f"The server redirects to a different host ({dst.netloc}). Set SP_BASE_URL to "
        f"{target} — most likely the farm's Alternate Access Mapping for this zone."
    )


class RedirectRefused(TransportError):
    """Base for the redirects this connector refuses to follow.

    Never retried, and never followed. A redirect here is a configuration fact,
    not a transient one: following it papers over a wrong ``SP_BASE_URL``, and
    the URLs this connector records — manifest roots, web URLs, state keys —
    would then disagree with the zone the data actually came from.
    """

    def __init__(self, summary: str, source: str, status: int, location: str) -> None:
        super().__init__(f"{summary}\n  {redirect_advice(source, location)}")
        self.source = source
        self.status = status
        self.location = location

    @property
    def suggested_base_url(self) -> str | None:
        return redirect_target(self.source, self.location)


class SoapRedirectError(RedirectRefused):
    """A SOAP POST was answered with a redirect, which silently destroys it.

    ``requests`` follows 301/302/303 by rewriting the method to ``GET`` and
    dropping the body along with ``Content-Type``. The redirected request
    therefore arrives at ``*.asmx`` as a bodyless GET, and IIS answers it with
    the ASMX service-description *page* — HTML with a 200 status. The SOAP layer
    then reports a missing result element, which points at the wrong thing
    entirely.

    The tell is that GET-based checks keep working while every SOAP call fails:
    the version probe redirects harmlessly, so the farm looks reachable right up
    until the first real operation.
    """

    def __init__(self, endpoint: str, status: int, location: str) -> None:
        # Advise on the *web* URL, not the endpoint: SP_BASE_URL names a site,
        # and "set SP_BASE_URL to https://host/_vti_bin/Webs.asmx" is nonsense.
        # We built this endpoint, so splitting it back apart is safe.
        super().__init__(
            f"{endpoint} answered HTTP {status} -> {location}",
            endpoint.split("/_vti_bin/", 1)[0],
            status,
            location,
        )
        self.endpoint = endpoint


class BaseUrlRedirectError(RedirectRefused):
    """``SP_BASE_URL`` is not where this farm answers.

    Raised from the auth probe, before any credential is sent, because every
    later diagnosis is worthless until this is settled: a farm that redirects
    every request cannot be asked which authentication schemes it offers.
    """

    def __init__(self, base_url: str, status: int, location: str) -> None:
        super().__init__(
            f"SP_BASE_URL {base_url} answered HTTP {status} -> {location}",
            base_url,
            status,
            location,
        )
        self.base_url = base_url


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
    #: ``Location`` of a 3xx. Set even for the forms-login case, which is a
    #: redirect that happens to be diagnosable further.
    redirect_to: str | None = None
    #: The URL that was probed, needed to say where a redirect leads *from*.
    probed_url: str | None = None
    #: The configured ``SP_AUTH_MODE``, so advice can avoid suggesting a change
    #: that is not one.
    configured_mode: str = ""

    @property
    def is_redirect(self) -> bool:
        """True when the server moved us rather than answering.

        A 3xx is *not* an answer to "which authentication schemes do you
        offer?". Treating it as one — a 302 is, after all, under 400 — is how
        this probe used to report a farm that merely redirects http to https as
        offering anonymous access.
        """
        return self.status is not None and self.status in REDIRECT_STATUS

    @property
    def suggested_mode(self) -> str | None:
        """The ``SP_AUTH_MODE`` this server appears to want, or ``None``."""
        lowered = {s.lower() for s in self.schemes}
        if "ntlm" in lowered or "negotiate" in lowered:
            # `integrated` covers both of these and stores no password, so it is
            # never something to be talked out of. Reporting "the server offers
            # ntlm" to someone already on integrated reads as advice to
            # downgrade, when the two are the same wire schemes.
            return "ntlm" if self.configured_mode != "integrated" else "integrated"
        if "basic" in lowered:
            return "basic"
        if self.forms_login_url:
            return None  # forms-based auth is out of scope
        if self.is_redirect:
            return None  # unknowable until SP_BASE_URL points at the real endpoint
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
        if self.is_redirect and self.redirect_to:
            return f"server redirects to {self.redirect_to} instead of answering. " + redirect_advice(
                self.probed_url or "", self.redirect_to
            )
        if not self.schemes:
            if self.is_redirect:
                return f"server answered HTTP {self.status} with no Location header"
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
            "redirect_to": self.redirect_to,
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
        #: Set by :meth:`probe_version`: did the winning request carry a credential?
        self.version_probe_authenticated: bool | None = None
        #: Set by :meth:`probe_version`: the sign-in or access-denied page we were
        #: sent to instead of the site, if we were.
        self.version_probe_denied_by: str | None = None
        self.request_count = 0
        self.bytes_received = 0
        #: Requests the diagnostics send outside :meth:`_send` — the auth probe,
        #: the NTLM domain lookup, the differential check. Counted separately so
        #: the footer reports what actually left the process; a probe that says
        #: "0 HTTP requests" after a round trip undermines every other number.
        self.side_channel_requests = 0
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
        if not settings.verify_ssl:
            # urllib3 warns once per *request*, which on a narrated probe buries
            # the narration in six identical paragraphs and makes a real warning
            # impossible to spot. The fact is worth stating once, loudly, and it
            # is already in the probe header and the log line below.
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            log.warning(
                "tls.verification_disabled",
                detail=(
                    "SP_VERIFY_SSL=false — certificates are not checked and the connection "
                    "is not protected against interception. Expected on a farm with a "
                    "self-signed or expired certificate; set SP_VERIFY_SSL=true once it chains."
                ),
            )

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
                describe_auth_failure(
                    response,
                    auth_mode=self.settings.auth_mode,
                    username=self.settings.username,
                )
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

    def _authenticated(self, response: requests.Response) -> bool:
        """Did this exchange actually put a credential on the wire?

        A 2xx is not proof. Where SharePoint permits anonymous reads the request
        is served without anyone being asked for anything, leaving the
        connection exactly as unauthenticated as it was.
        """
        headers = getattr(response.request, "headers", {}) or {}
        return "authorization" in {k.lower() for k in headers}

    def _prime_attempt(self, method: str, endpoint: str, **kwargs: Any) -> bool | None:
        """One priming attempt. ``True`` primed, ``False`` refused, ``None`` inconclusive."""
        try:
            self.limiter.acquire()
            self.side_channel_requests += 1
            response = self.session.request(
                method,
                endpoint,
                timeout=self.settings.timeout_seconds,
                allow_redirects=False,
                **kwargs,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.debug("ntlm_prime.failed", method=method, endpoint=endpoint, error=str(exc))
            return None

        # Refusal is checked first, and deliberately: a request that carried a
        # credential and was still refused means the credential was rejected,
        # which is the opposite of primed.
        if response.status_code in (401, 403):
            log.info(
                "ntlm_prime", method=method, endpoint=endpoint, status=response.status_code, primed=False
            )
            return False
        if self._authenticated(response):
            log.info("ntlm_prime", method=method, endpoint=endpoint, status=response.status_code, primed=True)
            return True
        log.debug("ntlm_prime.anonymous", method=method, endpoint=endpoint, status=response.status_code)
        return None

    def _prime_connection(self, endpoint: str, soap_action: str | None = None) -> bool:
        """Get an NTLM handshake done where it can succeed. ``True`` if it did.

        NTLM authenticates the TCP connection, not the request, and the
        handshake takes three round trips. ``requests-ntlm`` replays the **full
        body** on all three, where WinHTTP and .NET send the early legs empty
        and attach the body only to the final authenticated one. IIS commonly
        tears the connection down when it 401s a request carrying a body — the
        server logs a logon immediately followed by a logoff — so the legs land
        on different sockets and the negotiation can never complete.

        Two attempts, in order:

        1. **An empty POST** to the same endpoint. Same method, same URL, no
           body to provoke the teardown. Preferred because it differs from the
           real call in exactly one respect, so nothing method-specific can
           explain away the result.
        2. **A bodyless GET**, for farms that answer a contentless POST oddly.

        Either way the credential must actually be exercised: a request served
        anonymously proves nothing and primes nothing. And a refusal stops the
        whole thing — retrying then would spend a second failed authentication
        against an account that may have a lockout policy.
        """
        attempts: list[tuple[str, dict[str, Any]]] = []
        if soap_action is not None:
            attempts.append(
                (
                    "POST",
                    {
                        "data": b"",
                        "headers": {
                            "Content-Type": "text/xml; charset=utf-8",
                            "SOAPAction": f'"{soap_action}"',
                        },
                    },
                )
            )
        attempts.append(("GET", {}))

        for method, kwargs in attempts:
            outcome = self._prime_attempt(method, endpoint, **kwargs)
            if outcome is True:
                return True
            if outcome is False:
                return False  # refused outright: do not spend another attempt

        log.warning(
            "ntlm_prime.anonymous",
            endpoint=endpoint,
            detail=(
                "nothing we sent was challenged — this endpoint served us without asking "
                "for a credential, so there is no authenticated connection to reuse. The "
                "POST needs a credential the server is never asking for."
            ),
        )
        return False

    def post_soap(self, endpoint: str, body: bytes, soap_action: str, *, _primed: bool = False) -> bytes:
        """POST a SOAP envelope. Returns raw bytes; fault parsing lives in :mod:`soap`.

        Redirects are refused rather than followed: see :class:`SoapRedirectError`
        for why following one turns a SOAP call into an unrelated HTML page.

        A 401 gets one retry behind :meth:`_prime_connection`, and only when a
        bodyless GET to the same endpoint proves the credential is accepted
        there — see that method for why the POST alone can fail.
        """
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{soap_action}"',
        }
        try:
            response = self.request("POST", endpoint, data=body, headers=headers, allow_redirects=False)
        except AuthenticationError:
            eligible = (
                not _primed
                and self.settings.ntlm_prime_connection
                and self.settings.auth_mode in ("ntlm", "integrated")
            )
            if not eligible or not self._prime_connection(endpoint, soap_action):
                raise
            log.warning(
                "ntlm_prime.retry",
                endpoint=endpoint,
                detail=(
                    "the POST was refused, but a contentless request to the same endpoint "
                    "negotiated successfully; retrying the POST on the connection that "
                    "authenticated"
                ),
            )
            return self.post_soap(endpoint, body, soap_action, _primed=True)
        if response.status_code in REDIRECT_STATUS:
            location = response.headers.get("Location", "<no Location header>")
            log.error("soap.redirected", endpoint=endpoint, status=response.status_code, location=location)
            raise SoapRedirectError(endpoint, response.status_code, location)
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
            self.side_channel_requests += 1
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

        probe = AuthProbe(
            status=response.status_code,
            schemes=schemes,
            forms_login_url=forms_login,
            redirect_to=location or None,
            probed_url=target,
            configured_mode=self.settings.auth_mode,
        )
        log.info(
            "auth_probe",
            status=probe.status,
            schemes=schemes,
            suggested=probe.suggested_mode,
            redirect_to=probe.redirect_to,
        )
        return probe

    def discover_ntlm_domain(self, url: str | None = None) -> NtlmTarget | None:
        """Ask the server which domain it belongs to. Sends no credential.

        One unauthenticated round trip: we offer an NTLM Type 1, the server
        answers 401 with a Type 2, and the Type 2 names its domain. Works
        whatever ``SP_AUTH_MODE`` is set to, because it bypasses the session's
        auth handler entirely — which is the point, since the operator who needs
        this is usually the one whose auth mode is wrong.

        Returns ``None`` when the server does not offer NTLM or answers with
        something unparseable. Never raises: this is a diagnostic.
        """
        target = url or self.settings.base_url
        saved_auth = self.session.auth
        self.session.auth = None
        try:
            self.limiter.acquire()
            self.side_channel_requests += 1
            response = self.session.get(
                target,
                timeout=self.settings.timeout_seconds,
                allow_redirects=False,
                headers={"Authorization": f"NTLM {NTLM_NEGOTIATE}"},
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            log.debug("ntlm_domain.unreachable", error=str(exc))
            return None
        finally:
            self.session.auth = saved_auth

        header = response.headers.get("WWW-Authenticate", "")
        token = next(
            (part.strip()[5:] for part in header.split(",") if part.strip().lower().startswith("ntlm ")),
            None,
        )
        if not token:
            return None
        try:
            blob = base64.b64decode(token, validate=True)
        except (ValueError, binascii.Error):
            return None

        found = parse_ntlm_challenge(blob)
        if found is not None:
            log.info("ntlm_domain", **found.as_dict())
        return found

    def _status_of(self, method: str, url: str) -> tuple[str, bool]:
        """``(description, reached_it)`` for one authenticated request. Never raises.

        Bypasses :meth:`_send` deliberately: there a 401 is an exception, and
        here it is the measurement. Redirects are not followed, so a redirect to
        an access-denied page is visible as itself rather than as the 200 that
        page would have returned.
        """
        try:
            self.limiter.acquire()
            self.side_channel_requests += 1
            response = self.session.request(
                method, url, timeout=self.settings.timeout_seconds, allow_redirects=False
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            return type(exc).__name__, False

        location = response.headers.get("Location", "")
        if denied := denial_page(location):
            return f"{response.status_code} -> {denied}", False
        if location:
            return f"{response.status_code} -> {location}", response.status_code < 400
        return str(response.status_code), response.status_code < 400

    def diagnose_endpoint_auth(self, endpoint: str) -> list[str]:
        """Separate "this credential is refused" from "this *request* is refused".

        A 401 on a SOAP POST has three very different causes that look identical
        from one request. Two extra bodyless GETs tell them apart:

        =================  ================  ==============================================
        GET site root      GET the endpoint  Reading
        =================  ================  ==============================================
        ok                 ok                The credential is fine here — the **POST**
                                             is what fails. NTLM authenticates a
                                             connection, and IIS often closes it when it
                                             401s a request carrying a body, which fails
                                             the handshake regardless of the password.
        ok                 401               The account reaches the site but not
                                             ``_vti_bin`` — permissions on the web
                                             services, or a different auth provider
                                             configured on that virtual directory.
        401                401               The account cannot read this web at all.
                                             A SharePoint permissions job, not a
                                             connector one.
        =================  ================  ==============================================

        Read-only, three requests, and it runs where the failure happened rather
        than asking the operator to reproduce it by hand.
        """
        root = self.settings.base_url
        root_get, root_ok = self._status_of("GET", root)
        endpoint_get, endpoint_ok = self._status_of("GET", endpoint)

        lines = [
            "Differential check (the POST failed — do bodyless GETs?):",
            f"  GET {root} -> HTTP {root_get}",
            f"  GET {endpoint} -> HTTP {endpoint_get}",
        ]

        if root_ok and endpoint_ok:
            lines.append(
                "  => The credential is accepted for both. Only the POST fails, which "
                "points at the NTLM handshake over a request with a body rather than at "
                "the account. Ask for Kerberos/Negotiate, or try SP_AUTH_MODE=integrated."
            )
        elif root_ok:
            lines.append(
                "  => The account reaches the site but not _vti_bin. Ask the SharePoint "
                "admin whether the web services are restricted on this zone, and whether "
                "the account has Read on the root web."
            )
        else:
            lines.append(
                "  => The account cannot read this web at all. This is a SharePoint "
                "permissions question: grant it Read on the root web, then retry."
            )
        return lines

    def check_base_url(self, url: str | None = None) -> None:
        """Fail loudly when ``SP_BASE_URL`` is not where the farm answers.

        Reuses the auth probe's response rather than spending another request:
        the same unauthenticated, unredirected GET answers both questions, and
        the auth answer is meaningless while the redirect stands.
        """
        probe = self.probe_auth_schemes(url)
        self.raise_for_base_url_redirect(probe)

    @staticmethod
    def raise_for_base_url_redirect(probe: AuthProbe) -> None:
        """Turn a redirecting :class:`AuthProbe` into :class:`BaseUrlRedirectError`.

        Forms-based auth is left alone: that is a redirect too, but it needs its
        own diagnosis, and pointing ``SP_BASE_URL`` at a login page helps nobody.
        """
        if not probe.is_redirect or probe.forms_login_url or not probe.redirect_to:
            return
        if redirect_target(probe.probed_url or "", probe.redirect_to) is None:
            return  # same-origin redirect; the base URL itself is fine
        raise BaseUrlRedirectError(probe.probed_url or "", probe.status or 0, probe.redirect_to)

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

        # Where we actually ended up. A 200 proves a page was served, not that
        # it was the page we asked for: SharePoint answers "you may not read
        # this" by redirecting to an access-denied page that returns 200 and
        # carries the version header, so status alone cannot tell the two apart.
        self.version_probe_denied_by = denial_page(
            response.url, *(step.headers.get("Location", "") for step in response.history)
        )

        # Whether the credential was actually exercised, as opposed to the
        # request simply being allowed through. "login successful" asserted from
        # a 2xx alone is a false green: anonymous-readable farms, and redirects
        # to pages that need no login, both produce one without authenticating.
        self.version_probe_authenticated = "authorization" in {
            k.lower() for k in (getattr(response.request, "headers", {}) or {})
        }

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
