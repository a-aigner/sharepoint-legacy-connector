"""Retry policy, auth failure handling, rate limiting, and the version probe."""

from __future__ import annotations

import base64
import struct
import time
from pathlib import Path

import pytest
import requests
import responses

from conftest import WEB1, fixture_bytes, make_settings
from spconnect.transport import (
    AuthenticationError,
    BaseUrlRedirectError,
    NotFoundError,
    RateLimiter,
    RetryableTransportError,
    ServerVersion,
    SoapRedirectError,
    Transport,
    _looks_like_soap_fault,
    denial_page,
    describe_auth_failure,
    parse_ntlm_challenge,
    redirect_target,
)

ASMX_PATH = "/sites/service/_vti_bin/Lists.asmx"
ENDPOINT = f"{WEB1}/_vti_bin/Lists.asmx"


@pytest.fixture
def rsps():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        yield mock


@pytest.fixture
def tp(tmp_path: Path) -> Transport:
    return Transport(make_settings(tmp_path, max_retries=3))


# --------------------------------------------------------------------------- #
# retries
# --------------------------------------------------------------------------- #


def test_transient_5xx_is_retried_then_succeeds(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, status=503)
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    assert tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"
    assert len(rsps.calls) == 2


def test_retries_are_bounded(rsps, tp: Transport) -> None:
    for _ in range(5):
        rsps.add(responses.POST, ENDPOINT, status=500, body="not a fault")
    with pytest.raises(RetryableTransportError):
        tp.post_soap(ENDPOINT, b"<x/>", "op")
    assert len(rsps.calls) == 3  # max_retries


def test_connection_errors_are_retried(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, body=requests.ConnectionError("reset by peer"))
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    assert tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"


def test_timeouts_are_retried(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, body=requests.Timeout("too slow"))
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    assert tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failures_fail_fast_and_are_never_retried(rsps, tp: Transport, status: int) -> None:
    rsps.add(responses.POST, ENDPOINT, status=status)
    with pytest.raises(AuthenticationError) as excinfo:
        tp.post_soap(ENDPOINT, b"<x/>", "op")
    # tp runs SP_AUTH_MODE=anonymous, so nothing was sent — naming SP_USERNAME
    # here would point at a setting that is not the problem.
    assert "NONE SENT" in str(excinfo.value)
    assert "SP_AUTH_MODE" in str(excinfo.value)
    assert len(rsps.calls) == 1


def a_401(
    *, challenge: str | None = None, sent_credential: bool = True, connection: str | None = None
) -> requests.Response:
    """A 401 shaped like the ones this farm returns, without the auth machinery."""
    response = requests.Response()
    response.status_code = 401
    response.url = ENDPOINT
    response._content = b""
    if challenge:
        response.headers["WWW-Authenticate"] = challenge
    if connection:
        response.headers["Connection"] = connection
    request_headers = {"Authorization": "NTLM abc123"} if sent_credential else {}
    response.request = requests.Request("POST", ENDPOINT, headers=request_headers).prepare()
    return response


def test_a_rejected_credential_says_so_and_names_the_settings() -> None:
    message = describe_auth_failure(a_401(challenge="NTLM"), auth_mode="ntlm", username="pkober")
    assert "re-challenges with NTLM" in message
    assert "has no domain part" in message
    assert "SP_USERNAME" in message


def test_an_authorisation_failure_is_not_reported_as_a_bad_password() -> None:
    """401 *without* a challenge means accepted-then-denied: a permissions problem.

    This is the distinction the old message could not make, and the one that
    decides whether the next hour goes to passwords or to permissions.
    """
    message = describe_auth_failure(a_401(), auth_mode="ntlm", username="CONTOSO\\pkober")
    assert "likely ACCEPTED and then denied" in message
    assert "permissions on this web, before SP_USERNAME" in message
    assert "has no domain part" not in message


def test_a_credential_that_never_left_the_process_says_so() -> None:
    message = describe_auth_failure(a_401(sent_credential=False), auth_mode="anonymous", username="")
    assert "NONE SENT" in message


def test_a_closed_connection_is_called_out_for_ntlm() -> None:
    message = describe_auth_failure(
        a_401(challenge="NTLM", connection="close"), auth_mode="ntlm", username="CONTOSO\\p"
    )
    assert "Connection: close" in message
    assert "authenticates the connection" in message


def test_404_is_not_retried(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, status=404)
    with pytest.raises(NotFoundError):
        tp.post_soap(ENDPOINT, b"<x/>", "op")
    assert len(rsps.calls) == 1


def test_a_500_carrying_a_soap_fault_is_returned_not_retried(rsps, tp: Transport) -> None:
    # SharePoint reports application errors as HTTP 500 + <soap:Fault>. Retrying
    # those five times just annoys a twenty-year-old server.
    rsps.add(responses.POST, ENDPOINT, status=500, body=fixture_bytes("soap_fault.xml"))
    payload = tp.post_soap(ENDPOINT, b"<x/>", "op")
    assert b"faultstring" in payload
    assert len(rsps.calls) == 1


def test_looks_like_soap_fault() -> None:
    assert _looks_like_soap_fault(fixture_bytes("soap_fault.xml"))
    assert not _looks_like_soap_fault(fixture_bytes("lists_getlistcollection.xml"))
    assert not _looks_like_soap_fault(b"<html>500 Internal Server Error</html>")


# --------------------------------------------------------------------------- #
# headers
# --------------------------------------------------------------------------- #


def test_soap_headers_are_exact(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    tp.post_soap(ENDPOINT, b"<x/>", "http://schemas.microsoft.com/sharepoint/soap/GetListItems")
    headers = rsps.calls[0].request.headers
    assert headers["Content-Type"] == "text/xml; charset=utf-8"
    # The quotes around SOAPAction are required by SOAP 1.1 and by IIS here.
    assert headers["SOAPAction"] == '"http://schemas.microsoft.com/sharepoint/soap/GetListItems"'


# --------------------------------------------------------------------------- #
# version probe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("header", "major", "product", "tokens"),
    [
        ("12.0.0.6421", 12, "WSS 3.0 / MOSS 2007", True),
        ("6.0.2.6568", 6, "WSS 2.0 / SharePoint Portal Server 2003", False),
        ("14.0.0.6029", 14, "SharePoint 2010", True),
        ("15.0.0.4420", 15, "SharePoint 2013", True),
    ],
)
def test_version_probe_reads_the_build_number(
    rsps, tp: Transport, header: str, major: int, product: str, tokens: bool
) -> None:
    rsps.add(responses.HEAD, WEB1, status=200, headers={"MicrosoftSharePointTeamServices": header})
    version = tp.probe_version()
    assert (version.raw, version.major, version.product) == (header, major, product)
    assert version.supports_change_tokens is tokens
    assert version.as_dict()["product"] == product


def test_version_probe_falls_back_to_get_when_head_lacks_the_header(rsps, tp: Transport) -> None:
    rsps.add(responses.HEAD, WEB1, status=200)
    rsps.add(
        responses.GET, WEB1, status=200, headers={"MicrosoftSharePointTeamServices": "12.0.0.6421"}, body="x"
    )
    assert tp.probe_version().major == 12


def test_version_probe_survives_a_server_without_the_header(rsps, tp: Transport) -> None:
    rsps.add(responses.HEAD, WEB1, status=200)
    rsps.add(responses.GET, WEB1, status=200, body="x")
    version = tp.probe_version()
    assert version.raw is None
    assert version.major is None
    assert version.product == "unknown"
    assert version.supports_change_tokens is False


def test_unknown_major_is_reported_honestly() -> None:
    assert "unknown build major 99" in ServerVersion(raw="99.0", major=99).product


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #


def test_rate_limiter_spaces_requests_out() -> None:
    limiter = RateLimiter(requests_per_second=50)
    started = time.monotonic()
    for _ in range(4):
        limiter.acquire()
    # 4 acquisitions at 50/s cost at least 3 intervals.
    assert time.monotonic() - started >= 0.05


def test_rate_limiter_disabled_does_not_sleep() -> None:
    limiter = RateLimiter(requests_per_second=0)
    started = time.monotonic()
    for _ in range(100):
        limiter.acquire()
    assert time.monotonic() - started < 0.05


def test_the_limiter_applies_to_downloads_too(rsps, tmp_path: Path) -> None:
    tp = Transport(make_settings(tmp_path, requests_per_second=50))
    rsps.add(responses.GET, "http://sp/a.bin", body=b"x")
    rsps.add(responses.GET, "http://sp/b.bin", body=b"x")
    started = time.monotonic()
    tp.get("http://sp/a.bin")
    tp.get("http://sp/b.bin")
    assert time.monotonic() - started >= 0.02


# --------------------------------------------------------------------------- #
# session construction
# --------------------------------------------------------------------------- #


def test_anonymous_mode_sets_no_auth(tmp_path: Path) -> None:
    assert Transport(make_settings(tmp_path, auth_mode="anonymous")).session.auth is None


def test_basic_auth_is_wired(tmp_path: Path) -> None:
    tp = Transport(make_settings(tmp_path, auth_mode="basic", username="u", password="p"))
    assert isinstance(tp.session.auth, requests.auth.HTTPBasicAuth)


def test_ntlm_auth_is_wired(tmp_path: Path) -> None:
    from requests_ntlm import HttpNtlmAuth

    tp = Transport(make_settings(tmp_path, auth_mode="ntlm", username="CONTOSO\\u", password="p"))
    assert isinstance(tp.session.auth, HttpNtlmAuth)


def test_legacy_tls_adapter_is_mounted_only_when_asked(tmp_path: Path) -> None:
    from spconnect.transport import LegacyTLSAdapter

    plain = Transport(make_settings(tmp_path, allow_legacy_tls=False))
    assert not isinstance(plain.session.get_adapter("https://sp/"), LegacyTLSAdapter)

    legacy = Transport(make_settings(tmp_path, allow_legacy_tls=True))
    assert isinstance(legacy.session.get_adapter("https://sp/"), LegacyTLSAdapter)


def test_transport_closes_cleanly(tmp_path: Path) -> None:
    with Transport(make_settings(tmp_path)) as tp:
        assert tp.session is not None


# --------------------------------------------------------------------------- #
# auth scheme probe
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("header", "schemes", "mode"),
    [
        ("NTLM", ["NTLM"], "ntlm"),
        ("Negotiate, NTLM", ["Negotiate", "NTLM"], "ntlm"),
        ('Basic realm="SharePoint"', ["Basic"], "basic"),
        ('Negotiate, NTLM, Basic realm="x"', ["Negotiate", "NTLM", "Basic"], "ntlm"),
        ("Negotiate", ["Negotiate"], "ntlm"),
        ("NTLM, ntlm", ["NTLM"], "ntlm"),
    ],
)
def test_auth_probe_reads_the_offered_schemes(
    rsps, tp: Transport, header: str, schemes: list[str], mode: str
) -> None:
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": header})
    probe = tp.probe_auth_schemes()
    assert probe.schemes == schemes
    assert probe.suggested_mode == mode
    assert probe.status == 401


def test_auth_probe_sends_no_credentials_and_restores_session_auth(rsps, tmp_path: Path) -> None:
    tp = Transport(make_settings(tmp_path, auth_mode="basic", username="u", password="p"))
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "NTLM"})

    before = tp.session.auth
    tp.probe_auth_schemes()

    assert "Authorization" not in rsps.calls[0].request.headers
    assert tp.session.auth is before  # the real credential survives the probe


def test_auth_probe_detects_anonymous(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=200, body="<html>hello</html>")
    probe = tp.probe_auth_schemes()
    assert probe.schemes == []
    assert probe.suggested_mode == "anonymous"
    assert "anonymous" in probe.advice


def test_auth_probe_detects_forms_based_auth_by_redirect(rsps, tp: Transport) -> None:
    rsps.add(
        responses.GET,
        WEB1,
        status=302,
        headers={"Location": "http://sp/_layouts/login.aspx?ReturnUrl=%2f"},
    )
    probe = tp.probe_auth_schemes()
    assert probe.forms_login_url is not None
    assert probe.suggested_mode is None
    assert "NOT SUPPORTED" in probe.advice


def test_auth_probe_detects_forms_based_auth_by_body(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=200, body='<form action="/_layouts/login.aspx">')
    probe = tp.probe_auth_schemes()
    assert probe.forms_login_url is not None
    assert probe.suggested_mode is None


def test_auth_probe_survives_an_unreachable_server(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, body=requests.ConnectionError("no route to host"))
    probe = tp.probe_auth_schemes()
    assert probe.status is None
    assert probe.error is not None
    assert "could not determine" in probe.advice
    assert probe.suggested_mode is None


def test_kerberos_only_is_called_out(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "Negotiate"})
    assert "Kerberos only" in tp.probe_auth_schemes().advice


def test_auth_probe_is_manifest_ready(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "NTLM"})
    assert tp.probe_auth_schemes().as_dict()["suggested_mode"] == "ntlm"


def test_parse_auth_schemes_handles_a_header_with_parameters() -> None:
    from spconnect.transport import _parse_auth_schemes

    assert _parse_auth_schemes('Basic realm="SharePoint", Negotiate') == ["Basic", "Negotiate"]
    assert _parse_auth_schemes("") == []
    assert _parse_auth_schemes("   ") == []


@pytest.mark.parametrize(
    ("major", "throttled"), [(6, False), (12, False), (14, True), (15, True), (16, True), (None, False)]
)
def test_list_view_threshold_arrived_with_2010(major: int | None, throttled: bool) -> None:
    version = ServerVersion(raw=f"{major}.0.0.0" if major else None, major=major)
    assert version.has_list_view_threshold is throttled
    assert version.as_dict()["has_list_view_threshold"] is throttled


def test_sharepoint_2010_supports_everything_the_crawler_needs() -> None:
    version = ServerVersion(raw="14.0.4762.1000", major=14)
    assert version.product == "SharePoint 2010"
    assert version.supports_change_tokens is True
    assert version.supports_all_sub_web_collection is True


# --------------------------------------------------------------------------- #
# redirects
#
# A SharePoint 2010 farm addressed over http while IIS redirected to https
# produced every failure in this section. The connector reported a missing SOAP
# result element, then advised SP_AUTH_MODE=anonymous, and named the wrong
# layer twice before anyone looked at the base URL.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_soap_post_refuses_every_redirect_status(rsps, tp: Transport, status: int) -> None:
    rsps.add(responses.POST, ENDPOINT, status=status, headers={"Location": f"https://sp{ASMX_PATH}"})

    with pytest.raises(SoapRedirectError) as exc:
        tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert exc.value.status == status
    # Following it would rewrite POST to GET and drop the envelope; refusing it
    # means exactly one request leaves the process, and none is retried.
    assert len(rsps.calls) == 1


def test_soap_redirect_advice_survives_a_farm_that_rewrites_the_path(rsps, tp: Transport) -> None:
    # The real farm redirected /_vti_bin/Webs.asmx to /Webs.asmx — the target's
    # path is junk, so the advice must be built from the source path instead.
    rsps.add(responses.POST, ENDPOINT, status=302, headers={"Location": "https://sp/Lists.asmx"})

    with pytest.raises(SoapRedirectError) as exc:
        tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert exc.value.suggested_base_url == "https://sp/sites/service"
    assert "Set SP_BASE_URL to https://sp/sites/service" in str(exc.value)
    assert "_vti_bin" not in str(exc.value).split("\n")[1]  # not in the advice line


def test_soap_post_still_returns_a_normal_body(rsps, tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    assert tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"


@pytest.mark.parametrize(
    ("source", "location", "expected"),
    [
        # Scheme change on a root-level site: the case that started this.
        ("http://crm.example.de", "https://crm.example.de/Webs.asmx", "https://crm.example.de"),
        # Scheme change on a site collection: keep the path, swap the scheme.
        ("http://sp/sites/service", "https://sp/Webs.asmx", "https://sp/sites/service"),
        # Host change: an Alternate Access Mapping pointing elsewhere.
        ("http://sp/sites/service", "http://intranet/x", "http://intranet/sites/service"),
        # Same origin: the base URL is fine, the path is the server's business.
        ("http://sp/sites/service", "http://sp/sites/service/default.aspx", None),
        # Relative Location: same origin by definition.
        ("http://sp/sites/service", "/sites/service/default.aspx", None),
    ],
)
def test_redirect_target_keeps_the_source_path(source: str, location: str, expected: str | None) -> None:
    assert redirect_target(source, location) == expected


def test_auth_probe_does_not_call_a_redirect_anonymous(rsps, tp: Transport) -> None:
    """A 302 is under 400, which used to be read as "answered without a challenge"."""
    rsps.add(responses.GET, WEB1, status=302, headers={"Location": "https://sp/sites/service"})

    probe = tp.probe_auth_schemes()

    assert probe.is_redirect is True
    assert probe.suggested_mode is None, "a redirect is not an offer of anonymous access"
    assert "anonymous" not in probe.advice
    assert "https://sp/sites/service" in probe.advice
    assert probe.as_dict()["redirect_to"] == "https://sp/sites/service"


def test_auth_probe_still_reports_genuine_anonymous_access(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=200, body="<html/>")

    probe = tp.probe_auth_schemes()

    assert probe.is_redirect is False
    assert probe.suggested_mode == "anonymous"


def test_check_base_url_rejects_a_redirecting_farm(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=302, headers={"Location": "https://sp/sites/service"})

    with pytest.raises(BaseUrlRedirectError) as exc:
        tp.check_base_url()

    assert exc.value.suggested_base_url == "https://sp/sites/service"
    assert exc.value.status == 302


def test_check_base_url_accepts_a_farm_that_answers(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "NTLM"})
    tp.check_base_url()  # must not raise


def test_check_base_url_leaves_forms_auth_to_its_own_diagnosis(rsps, tp: Transport) -> None:
    # Also a redirect, but "point SP_BASE_URL at the login page" helps nobody.
    rsps.add(
        responses.GET,
        WEB1,
        status=302,
        headers={"Location": "http://sp/_layouts/login.aspx?ReturnUrl=%2f"},
    )

    tp.check_base_url()  # must not raise

    probe = tp.probe_auth_schemes()
    assert probe.forms_login_url is not None
    assert "forms-based auth" in probe.advice


def test_check_base_url_ignores_a_same_origin_redirect(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=302, headers={"Location": f"{WEB1}/default.aspx"})
    tp.check_base_url()  # must not raise


# --------------------------------------------------------------------------- #
# NTLM domain discovery
#
# "Use DOMAIN\user" is advice nobody can act on without knowing DOMAIN, and the
# operators who hit it are usually the ones who cannot find out. The server will
# say, unauthenticated, on any 401.
# --------------------------------------------------------------------------- #


def ntlm_challenge(
    *,
    netbios_domain: str | None = "CONTOSO",
    dns_domain: str | None = "contoso.local",
    computer: str | None = "SP2010",
    unicode_flag: bool = True,
) -> bytes:
    """Build a Type 2 the way a domain-joined IIS does (MS-NLMP 2.2.1.2)."""
    name = (netbios_domain or "").encode("utf-16-le")
    pairs = b""
    for av_id, value in ((2, netbios_domain), (1, computer), (4, dns_domain)):
        if value:
            encoded = value.encode("utf-16-le")
            pairs += struct.pack("<HH", av_id, len(encoded)) + encoded
    pairs += struct.pack("<HH", 0, 0)  # MsvAvEOL

    name_offset = 48
    info_offset = name_offset + len(name)
    header = (
        b"NTLMSSP\x00"
        + struct.pack("<I", 2)
        + struct.pack("<HHI", len(name), len(name), name_offset)
        + struct.pack("<I", 0x00088205 if unicode_flag else 0x00088204)
        + b"\x11" * 8  # server challenge
        + b"\x00" * 8  # reserved
        + struct.pack("<HHI", len(pairs), len(pairs), info_offset)
    )
    return header + name + pairs


def test_the_domain_is_read_out_of_the_challenge() -> None:
    target = parse_ntlm_challenge(ntlm_challenge())
    assert target is not None
    assert target.netbios_domain == "CONTOSO"
    assert target.dns_domain == "contoso.local"
    assert target.netbios_computer == "SP2010"
    assert target.username_hint == "CONTOSO\\<user>"


def test_a_server_with_only_a_dns_domain_still_gives_a_usable_hint() -> None:
    target = parse_ntlm_challenge(ntlm_challenge(netbios_domain=None, computer=None))
    assert target is not None
    assert target.username_hint == "<user>@contoso.local"


@pytest.mark.parametrize(
    ("label", "blob"),
    [
        ("empty", b""),
        ("not ntlm", b"<html>401</html>"),
        ("truncated header", b"NTLMSSP\x00" + struct.pack("<I", 2) + b"\x00" * 8),
        ("a Type 1, not a Type 2", b"NTLMSSP\x00" + struct.pack("<II", 1, 0) + b"\x00" * 40),
        (
            "offsets past the end",
            b"NTLMSSP\x00" + struct.pack("<I", 2) + struct.pack("<HHI", 400, 400, 9999) + b"\x00" * 36,
        ),
    ],
)
def test_a_malformed_challenge_yields_none_rather_than_raising(label: str, blob: bytes) -> None:
    assert parse_ntlm_challenge(blob) is None, label


def test_discover_ntlm_domain_asks_the_server_without_credentials(rsps, tp: Transport) -> None:
    token = base64.b64encode(ntlm_challenge()).decode()
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": f"NTLM {token}"})

    target = tp.discover_ntlm_domain()

    assert target is not None and target.netbios_domain == "CONTOSO"
    sent = rsps.calls[0].request.headers
    assert sent["Authorization"].startswith("NTLM ")
    # A Type 1 carries no identity — that is what makes this safe to run always.
    assert base64.b64decode(sent["Authorization"][5:]).startswith(b"NTLMSSP\x00")


def test_discover_ntlm_domain_is_quiet_when_the_server_does_not_offer_ntlm(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "Basic realm='x'"})
    assert tp.discover_ntlm_domain() is None


def test_the_configured_scheme_not_being_offered_is_named_as_such() -> None:
    """SP_AUTH_MODE=basic against an NTLM-only server: the credential never ran."""
    message = describe_auth_failure(a_401(challenge="NTLM"), auth_mode="basic", username="pkober")
    assert "does not offer basic at all" in message
    assert "never tried" in message
    # Blaming the password here would send someone to reset an account that is fine.
    assert "SP_PASSWORD" not in message


def test_negotiate_counts_as_offering_ntlm() -> None:
    message = describe_auth_failure(a_401(challenge="Negotiate"), auth_mode="ntlm", username="CONTOSO\\p")
    assert "does not offer" not in message


# --------------------------------------------------------------------------- #
# authenticated, but not authorised
#
# IIS decides whether you are who you say you are, and answers 401 when it
# doubts you. SharePoint decides whether that identity may read this, and
# answers a browser request by redirecting to a page that returns 200. A status
# check cannot tell the second one from success.
# --------------------------------------------------------------------------- #


DENIED_URL = "http://sp/sites/service/_layouts/AccessDenied.aspx?Source=%2F"


@pytest.mark.parametrize(
    "url",
    [
        DENIED_URL,
        "http://sp/_layouts/login.aspx?ReturnUrl=%2f",
        "http://sp/_forms/signin.aspx",
        "HTTP://SP/_LAYOUTS/ACCESSDENIED.ASPX",  # case is the server's business
    ],
)
def test_denial_pages_are_recognised(url: str) -> None:
    assert denial_page(url) == url


@pytest.mark.parametrize("url", ["http://sp/sites/service", "http://sp/default.aspx", ""])
def test_ordinary_pages_are_not_mistaken_for_denials(url: str) -> None:
    assert denial_page(url) is None


def test_landing_on_access_denied_is_recorded_by_the_version_probe(rsps, tp: Transport) -> None:
    """The page answers 200 with the version header, so status alone reads as success."""
    rsps.add(responses.HEAD, WEB1, status=302, headers={"Location": DENIED_URL})
    rsps.add(
        responses.HEAD,
        DENIED_URL,
        status=200,
        headers={"MicrosoftSharePointTeamServices": "14.0.0.7149"},
    )

    version = tp.probe_version()

    assert version.raw == "14.0.0.7149"  # the header really is there
    assert tp.version_probe_denied_by is not None
    assert "AccessDenied.aspx" in tp.version_probe_denied_by


def test_a_normal_landing_is_not_flagged(rsps, tp: Transport) -> None:
    rsps.add(responses.HEAD, WEB1, status=200, headers={"MicrosoftSharePointTeamServices": "14.0"})
    tp.probe_version()
    assert tp.version_probe_denied_by is None


def test_the_differential_does_not_count_a_denial_redirect_as_reaching_the_site(rsps, tp: Transport) -> None:
    rsps.add(responses.GET, WEB1, status=302, headers={"Location": DENIED_URL})
    rsps.add(responses.GET, ENDPOINT, status=401)

    lines = "\n".join(tp.diagnose_endpoint_auth(ENDPOINT))

    assert "AccessDenied.aspx" in lines
    # Counting that 302 as success would have read this as "_vti_bin is
    # restricted" when in fact the account cannot read the site at all.
    assert "cannot read this web at all" in lines


# --------------------------------------------------------------------------- #
# NTLM connection priming
#
# Confirmed on the reported farm: a browser and our own client can both GET
# /_vti_bin/Webs.asmx, and the SOAP POST to the same URL is refused. NTLM
# authenticates a connection, and IIS drops it when it 401s a request carrying
# a body, so the handshake legs land on different sockets.
# --------------------------------------------------------------------------- #


class CompletedHandshake:
    """Stands in for a finished NTLM negotiation: the request carries a credential.

    The real handshake cannot run against `responses` — requests-ntlm reaches
    for the TLS socket to build a channel binding token and there is no socket.
    What matters to priming is only whether the winning request was
    authenticated, which this reproduces exactly.
    """

    def __call__(self, request):
        request.headers["Authorization"] = "NTLM <negotiated>"
        return request


@pytest.fixture
def ntlm_tp(tmp_path: Path) -> Transport:
    transport = Transport(
        make_settings(tmp_path, auth_mode="ntlm", username="CONTOSO\\p", password="pw", max_retries=1)
    )
    transport.session.auth = CompletedHandshake()
    return transport


def test_a_post_refused_where_priming_succeeds_is_retried_and_works(rsps, ntlm_tp) -> None:
    """The real POST fails, an empty POST negotiates, the real POST then works."""
    rsps.add(responses.POST, ENDPOINT, status=401)  # body kills the handshake
    rsps.add(responses.POST, ENDPOINT, status=500, body="<fault/>")  # empty POST: negotiates
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")

    assert ntlm_tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"

    assert [c.request.method for c in rsps.calls] == ["POST", "POST", "POST"]
    # The priming POST must carry no body — that is the entire point of it.
    assert not rsps.calls[1].request.body
    assert rsps.calls[2].request.body == b"<x/>"


def test_priming_falls_back_to_a_get(rsps, ntlm_tp) -> None:
    """Some farms answer a contentless POST oddly; a bodyless GET still primes."""
    rsps.add(responses.POST, ENDPOINT, status=401)
    # IIS drops the connection on the contentless POST: nothing was established.
    rsps.add(responses.POST, ENDPOINT, body=requests.ConnectionError("reset by peer"))
    rsps.add(responses.GET, ENDPOINT, status=200, body="<html/>")
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")

    assert ntlm_tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"
    assert [c.request.method for c in rsps.calls] == ["POST", "POST", "GET", "POST"]


def test_a_genuinely_refused_credential_is_not_retried(rsps, ntlm_tp) -> None:
    """A credential that is sent and still refused is rejected, not unprimed.

    Retrying there would spend a second failed authentication against an account
    that may well have a lockout policy.
    """
    rsps.add(responses.POST, ENDPOINT, status=401)
    rsps.add(responses.POST, ENDPOINT, status=401)

    with pytest.raises(AuthenticationError):
        ntlm_tp.post_soap(ENDPOINT, b"<x/>", "op")

    # One priming attempt, no fallback, and no second real POST.
    assert [c.request.method for c in rsps.calls] == ["POST", "POST"]


def test_priming_is_attempted_only_once(rsps, ntlm_tp) -> None:
    """A farm that 401s the POST even after priming must not loop."""
    rsps.add(responses.POST, ENDPOINT, status=401)
    rsps.add(responses.POST, ENDPOINT, status=200)
    rsps.add(responses.POST, ENDPOINT, status=401)

    with pytest.raises(AuthenticationError):
        ntlm_tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert [c.request.method for c in rsps.calls] == ["POST", "POST", "POST"]


def test_nothing_being_challenged_does_not_count_as_priming(rsps, tmp_path: Path) -> None:
    """A 2xx is not proof of a handshake.

    Where SharePoint permits anonymous access the request is served without
    anyone being asked for anything, so the connection is left exactly as
    unauthenticated as it was. Retrying the POST on it would change nothing and
    hide the real problem — that the server is not asking for a credential.
    """
    tp = Transport(make_settings(tmp_path, auth_mode="ntlm", username="u", password="p"))
    tp.session.auth = None  # nothing attaches a credential: everything is anonymous
    rsps.add(responses.POST, ENDPOINT, status=401)
    rsps.add(responses.POST, ENDPOINT, status=200)
    rsps.add(responses.GET, ENDPOINT, status=200, body="<html>anyone may read this</html>")

    with pytest.raises(AuthenticationError):
        tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert [c.request.method for c in rsps.calls] == ["POST", "POST", "GET"]


def test_priming_can_be_switched_off(rsps, tmp_path: Path) -> None:
    tp = Transport(
        make_settings(tmp_path, auth_mode="ntlm", username="u", password="p", ntlm_prime_connection=False)
    )
    rsps.add(responses.POST, ENDPOINT, status=401)

    with pytest.raises(AuthenticationError):
        tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert [c.request.method for c in rsps.calls] == ["POST"]


def test_priming_does_not_apply_to_password_schemes(rsps, tmp_path: Path) -> None:
    """Basic sends the credential on every request; there is no connection to prime."""
    tp = Transport(make_settings(tmp_path, auth_mode="basic", username="u", password="p"))
    rsps.add(responses.POST, ENDPOINT, status=401)

    with pytest.raises(AuthenticationError):
        tp.post_soap(ENDPOINT, b"<x/>", "op")

    assert [c.request.method for c in rsps.calls] == ["POST"]


def test_a_healthy_farm_pays_nothing_for_this(rsps, ntlm_tp: Transport) -> None:
    rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
    assert ntlm_tp.post_soap(ENDPOINT, b"<x/>", "op") == b"<ok/>"
    assert len(rsps.calls) == 1


def test_a_401_with_no_challenge_to_ntlm_is_a_handshake_that_never_started() -> None:
    """Matches what the farm's own server log shows: requests, but no credentials.

    NTLM sends nothing until challenged. A 401 carrying no WWW-Authenticate
    ends the exchange before it begins, so nothing the client does can put a
    credential on the wire — and blaming SP_AUTH_MODE for "not attaching one"
    points at the one thing that is configured correctly.
    """
    message = describe_auth_failure(a_401(sent_credential=False), auth_mode="ntlm", username="pkober")
    assert "handshake never began" in message
    assert "no credentials" in message
    assert "SP_NTLM_PRIME_CONNECTION" in message
    assert "the server wanted a credential" not in message


def test_anonymous_mode_still_gets_the_plain_advice() -> None:
    message = describe_auth_failure(a_401(sent_credential=False), auth_mode="anonymous", username="")
    assert "SP_AUTH_MODE is 'anonymous'" in message
