"""Step narration, trace logging, and the secret-scrubbing guarantee.

The password test is the important one here: body logging exists precisely to
dump what the server said, and that is exactly when a credential is most likely
to slip into a log file.
"""

from __future__ import annotations

import base64
import logging
import stat
from pathlib import Path

import pytest
import responses

from conftest import WEB1, FakeFarm, make_settings
from spconnect.config import (
    REDACTED,
    get_logger,
    register_secret,
    scrub,
    scrub_value,
    setup_logging,
)
from spconnect.console import StepReporter, format_bytes, truncate
from spconnect.transport import SENSITIVE_HEADERS, Transport, redact_headers

ENDPOINT = f"{WEB1}/_vti_bin/Lists.asmx"


# --------------------------------------------------------------------------- #
# secret scrubbing
# --------------------------------------------------------------------------- #


def test_registered_secrets_are_scrubbed_from_text() -> None:
    register_secret("hunter2-supersecret")
    assert scrub("body says hunter2-supersecret here") == f"body says {REDACTED} here"


def test_trivially_short_values_are_not_registered() -> None:
    # Registering "a" would redact every letter 'a' in every log line.
    register_secret("ab")
    assert scrub("ab") == "ab"


def test_constructing_a_transport_registers_the_password(tmp_path: Path) -> None:
    Transport(make_settings(tmp_path, auth_mode="basic", username="u", password="pw-from-settings"))
    assert scrub("leaked pw-from-settings") == f"leaked {REDACTED}"


def test_the_password_never_reaches_a_body_log(tmp_path: Path, capsys) -> None:
    settings = make_settings(
        tmp_path,
        auth_mode="basic",
        username="CONTOSO\\svc",
        password="hunter2-in-body",
        log_bodies=True,
        log_level="DEBUG",
    )
    setup_logging("DEBUG", "console")
    transport = Transport(settings)

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        # A server that echoes the credential back — the nightmare case.
        rsps.add(responses.POST, ENDPOINT, status=200, body="<ok>hunter2-in-body</ok>")
        transport.post_soap(ENDPOINT, b"<envelope>hunter2-in-body</envelope>", "op")
    transport.close()

    captured = capsys.readouterr()
    assert "hunter2-in-body" not in captured.err
    assert "hunter2-in-body" not in captured.out
    # …and not in the trace file either, which is the second control.
    trace = settings.resolved_trace_file.read_text(encoding="utf-8")
    assert "hunter2-in-body" not in trace
    assert REDACTED in trace


def test_secrets_nested_in_structures_are_scrubbed() -> None:
    register_secret("nested-secret-value")
    assert scrub_value({"a": [{"pw": "nested-secret-value"}]}) == {"a": [{"pw": REDACTED}]}
    assert scrub_value(("nested-secret-value",)) == (REDACTED,)


def test_rendered_tracebacks_are_scrubbed(capsys) -> None:
    # format_exc_info renders the traceback into a string; scrubbing has to run
    # after it or a credential in an exception message escapes untouched.
    setup_logging("DEBUG", "console")
    register_secret("traceback-secret")
    log = get_logger("t")
    try:
        raise ValueError("failed with password traceback-secret")
    except ValueError:
        log.debug("boom", exc_info=True)
    err = capsys.readouterr().err
    assert "traceback-secret" not in err
    assert REDACTED in err
    setup_logging("CRITICAL", "console")


def test_base64_basic_auth_material_is_scrubbed() -> None:
    # The plaintext password never appears in a Basic blob, so a verbatim
    # substring match would miss it entirely.
    register_secret("b64pass", username="CONTOSO\\svc")
    blob = base64.b64encode(b"CONTOSO\\svc:b64pass").decode()
    assert scrub(f"Basic {blob}") == f"Basic {REDACTED}"


def test_url_encoded_passwords_are_scrubbed() -> None:
    register_secret("p@ss w0rd")
    assert "p%40ss%20w0rd" not in scrub("http://sp/x?pw=p%40ss%20w0rd")


def test_headers_are_allowlisted_not_denylisted() -> None:
    # A denylist cannot cover a header nobody thought of.
    redacted = redact_headers(
        {
            "Content-Type": "text/xml",
            "X-Forwarded-Authorization": "Basic c2VjcmV0",
            "X-Some-Vendor-Token": "abc123",
        }
    )
    assert redacted["Content-Type"] == "text/xml"
    assert redacted["X-Forwarded-Authorization"] == "***REDACTED***"
    assert redacted["X-Some-Vendor-Token"] == "***REDACTED***"


def test_www_authenticate_keeps_schemes_but_drops_the_token() -> None:
    # The value can carry a Negotiate/GSSAPI token; the schemes are the useful part.
    out = redact_headers({"WWW-Authenticate": "Negotiate YIIFtAYGKwYBBQUCoIIFqDC, NTLM"})
    assert out["WWW-Authenticate"] == "Negotiate, NTLM"
    assert "YIIFtA" not in out["WWW-Authenticate"]


def test_authorization_headers_are_never_logged() -> None:
    redacted = redact_headers({"Authorization": "Basic Q09OVE9TTzpodW50ZXIy", "Content-Type": "text/xml"})
    assert redacted["Authorization"] == "***REDACTED***"
    assert redacted["Content-Type"] == "text/xml"


@pytest.mark.parametrize("header", sorted(SENSITIVE_HEADERS))
def test_every_sensitive_header_is_covered(header: str) -> None:
    assert redact_headers({header.title(): "secret"})[header.title()] == "***REDACTED***"


# --------------------------------------------------------------------------- #
# trace logging
# --------------------------------------------------------------------------- #


def test_requests_and_responses_are_logged_at_debug(tmp_path: Path, capsys) -> None:
    setup_logging("DEBUG", "console")
    transport = Transport(make_settings(tmp_path))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")

    err = capsys.readouterr().err
    assert "http.request" in err
    assert "http.response" in err
    assert "status=200" in err


def test_no_trace_file_is_created_unless_asked(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, log_bodies=False)
    transport = Transport(settings)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<distinctive-marker/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    transport.close()
    assert transport.trace is None
    assert not settings.resolved_trace_file.exists()


def test_bodies_never_reach_the_log_stream(tmp_path: Path, capsys) -> None:
    # stderr is the stream most likely to be redirected somewhere shared, so
    # bodies must not travel on it even when capture is switched on.
    setup_logging("DEBUG", "console")
    settings = make_settings(tmp_path, log_bodies=True)
    transport = Transport(settings)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<distinctive-marker/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    transport.close()

    captured = capsys.readouterr()
    assert "distinctive-marker" not in captured.err
    assert "distinctive-marker" in settings.resolved_trace_file.read_text(encoding="utf-8")
    setup_logging("CRITICAL", "console")


def test_the_trace_file_is_owner_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, log_bodies=True)
    transport = Transport(settings)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<x/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    transport.close()

    mode = stat.S_IMODE(settings.resolved_trace_file.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_trace_entries_are_truncated(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, log_bodies=True, log_body_chars=50)
    transport = Transport(settings)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="y" * 5000)
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    transport.close()

    trace = settings.resolved_trace_file.read_text(encoding="utf-8")
    assert "more chars truncated" in trace
    assert "y" * 200 not in trace


def test_trace_records_both_directions(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, log_bodies=True)
    transport = Transport(settings)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<response-marker/>")
        transport.post_soap(ENDPOINT, b"<request-marker/>", "op")
    transport.close()

    trace = settings.resolved_trace_file.read_text(encoding="utf-8")
    assert "REQUEST" in trace and "RESPONSE" in trace
    assert "<request-marker/>" in trace and "<response-marker/>" in trace
    assert transport.trace is not None and transport.trace.entries == 2


def test_transport_counts_requests_and_bytes(tmp_path: Path) -> None:
    transport = Transport(make_settings(tmp_path))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="12345")
        rsps.add(responses.POST, ENDPOINT, status=200, body="678")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    assert transport.request_count == 2
    assert transport.bytes_received == 8


def test_log_level_actually_filters(tmp_path: Path, capsys) -> None:
    setup_logging("WARNING", "console")
    transport = Transport(make_settings(tmp_path))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")
    assert "http.request" not in capsys.readouterr().err
    setup_logging("CRITICAL", "console")


def test_json_log_format_is_machine_readable(tmp_path: Path, capsys) -> None:
    import json

    setup_logging("DEBUG", "json")
    transport = Transport(make_settings(tmp_path))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.POST, ENDPOINT, status=200, body="<ok/>")
        transport.post_soap(ENDPOINT, b"<x/>", "op")

    lines = [x for x in capsys.readouterr().err.splitlines() if x.strip().startswith("{")]
    assert lines
    parsed = [json.loads(x) for x in lines]
    assert any(p.get("event") == "http.response" for p in parsed)
    assert all("timestamp" in p and "level" in p for p in parsed)
    setup_logging("CRITICAL", "console")


def test_unknown_log_level_falls_back_to_info() -> None:
    setup_logging("NOT-A-LEVEL", "console")
    assert isinstance(logging.getLevelName("INFO"), int)
    setup_logging("CRITICAL", "console")


# --------------------------------------------------------------------------- #
# step narration
# --------------------------------------------------------------------------- #


def test_steps_are_numbered_and_aligned(capsys) -> None:
    reporter = StepReporter(total=2)
    with reporter.step("First thing") as st:
        st.detail("detail here")
    with reporter.step("Second thing"):
        pass
    reporter.done()

    out = capsys.readouterr().out
    assert "[1/2] First thing" in out
    assert "[2/2] Second thing" in out
    assert "OK" in out
    assert "detail here" in out
    assert "All steps OK" in out


def test_a_failing_step_is_reported_and_re_raised(capsys) -> None:
    reporter = StepReporter(total=1)
    with pytest.raises(ValueError), reporter.step("Doomed"):
        raise ValueError("boom")

    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "ValueError: boom" in out
    assert reporter.failed == ["Doomed"]


def test_notes_are_printed_indented_under_the_step(capsys) -> None:
    reporter = StepReporter()
    with reporter.step("With notes") as st:
        st.note("line one")
        st.note("line two")
    out = capsys.readouterr().out
    assert "      line one" in out
    assert "      line two" in out


def test_a_disabled_reporter_prints_nothing(capsys) -> None:
    reporter = StepReporter(enabled=False, total=1)
    with reporter.step("Silent") as st:
        st.detail("x")
        st.note("y")
    reporter.done()
    assert capsys.readouterr().out == ""


def test_done_names_the_failed_steps(capsys) -> None:
    reporter = StepReporter(total=1)
    with pytest.raises(RuntimeError), reporter.step("Broken"):
        raise RuntimeError("x")
    reporter.done()
    assert "1 step(s) FAILED" in capsys.readouterr().out


def test_an_early_failure_says_which_steps_never_ran(capsys) -> None:
    """A run that stopped at step 2 gets read as evidence about step 5.

    That is worse than useless while a setting is being tested: the operator
    concludes the flag did not help, from a run that never reached the code the flag
    controls. It happened — a scheme change failed the base-URL check at step 2 and
    the NTLM option under test was never exercised.
    """
    reporter = StepReporter(total=8)
    with reporter.step("First"):
        pass
    with pytest.raises(RuntimeError), reporter.step("Second"):
        raise RuntimeError("redirected")
    reporter.done()

    out = capsys.readouterr().out
    assert "Steps 3-8 never ran" in out
    assert "including any setting they would have exercised" in out


def test_a_failure_on_the_last_step_claims_nothing_was_skipped(capsys) -> None:
    reporter = StepReporter(total=2)
    with reporter.step("First"):
        pass
    with pytest.raises(RuntimeError), reporter.step("Second"):
        raise RuntimeError("x")
    reporter.done()

    assert "never ran" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("count", "expected"), [(512, "512 B"), (2048, "2.0 KB"), (5 * 1024 * 1024, "5.0 MB")]
)
def test_format_bytes(count: int, expected: str) -> None:
    assert format_bytes(count) == expected


def test_truncate_reports_what_it_dropped() -> None:
    assert truncate("abc", 10) == "abc"
    long = truncate("x" * 100, 10)
    assert long.startswith("x" * 10)
    assert "90 more chars" in long


# --------------------------------------------------------------------------- #
# end to end through the CLI
# --------------------------------------------------------------------------- #


def test_verbose_flag_turns_on_request_logging(tmp_path: Path, farm: FakeFarm) -> None:
    from typer.testing import CliRunner

    from spconnect.cli import app

    env = tmp_path / ".env"
    env.write_text(
        f"SP_BASE_URL={WEB1}\nSP_AUTH_MODE=anonymous\nSP_ALLOW_LEGACY_TLS=false\n"
        "SP_REQUESTS_PER_SECOND=10000\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(app, ["--env-file", str(env), "-vv", "probe"])
    assert result.exit_code == 0
    setup_logging("CRITICAL", "console")


# --------------------------------------------------------------------------- #
# integrated auth — the control, as opposed to the mitigation
# --------------------------------------------------------------------------- #


def test_integrated_mode_registers_no_secret(tmp_path: Path, monkeypatch) -> None:
    import sys as _sys
    import types

    fake = types.ModuleType("requests_negotiate_sspi")
    fake.HttpNegotiateAuth = lambda: "SSPI-AUTH"  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "requests_negotiate_sspi", fake)

    settings = make_settings(tmp_path, auth_mode="integrated", password="never-used-secret")
    assert settings.needs_password is False
    transport = Transport(settings)

    assert transport.session.auth == "SSPI-AUTH"
    # Nothing to scrub, because nothing was ever registered.
    assert scrub("never-used-secret") == "never-used-secret"


def test_integrated_prefers_windows_sspi_then_kerberos(monkeypatch) -> None:
    import sys as _sys
    import types

    from spconnect.transport import build_integrated_auth

    monkeypatch.setitem(_sys.modules, "requests_negotiate_sspi", None)
    gssapi = types.ModuleType("requests_gssapi")
    gssapi.HTTPSPNEGOAuth = lambda: "KERBEROS-AUTH"  # type: ignore[attr-defined]
    monkeypatch.setitem(_sys.modules, "requests_gssapi", gssapi)

    assert build_integrated_auth() == "KERBEROS-AUTH"


def test_integrated_without_a_provider_says_exactly_what_to_install(monkeypatch) -> None:
    import sys as _sys

    from spconnect.transport import INTEGRATED_PROVIDERS, IntegratedAuthUnavailable, build_integrated_auth

    for module_name, _cls, _extra in INTEGRATED_PROVIDERS:
        monkeypatch.setitem(_sys.modules, module_name, None)

    with pytest.raises(IntegratedAuthUnavailable) as excinfo:
        build_integrated_auth()

    message = str(excinfo.value)
    assert "spconnect[windows]" in message
    assert "spconnect[kerberos]" in message
    assert "kinit" in message


@pytest.mark.parametrize(
    ("mode", "needs"), [("integrated", False), ("anonymous", False), ("ntlm", True), ("basic", True)]
)
def test_only_password_modes_hold_a_secret(tmp_path: Path, mode: str, needs: bool) -> None:
    assert make_settings(tmp_path, auth_mode=mode).needs_password is needs


def test_basic_auth_warns_that_it_transmits_the_password(tmp_path: Path, capsys) -> None:
    setup_logging("WARNING", "console")
    Transport(make_settings(tmp_path, auth_mode="basic", username="u", password="pw12345"))
    err = capsys.readouterr().err
    assert "auth.basic" in err
    assert "integrated" in err
    setup_logging("CRITICAL", "console")


def test_captured_error_bodies_are_owner_only(tmp_path: Path) -> None:
    from spconnect.soap import SoapResponseError

    exc = SoapResponseError("GetListItems", "boom", body=b"<html>Set-Cookie leaked here</html>")
    target = exc.save_body(tmp_path / "landing" / "_last_bad_response.xml")
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_bytes() == b"<html>Set-Cookie leaked here</html>"
