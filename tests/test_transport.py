"""Retry policy, auth failure handling, rate limiting, and the version probe."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests
import responses

from conftest import WEB1, fixture_bytes, make_settings
from spconnect.transport import (
    AuthenticationError,
    NotFoundError,
    RateLimiter,
    RetryableTransportError,
    ServerVersion,
    Transport,
    _looks_like_soap_fault,
)

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
    assert "SP_USERNAME" in str(excinfo.value)
    assert len(rsps.calls) == 1


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
