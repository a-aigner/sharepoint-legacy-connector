"""CLI surface: exit codes, flag precedence, and the artifacts each command writes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import responses
from typer.testing import CliRunner

from conftest import CASES, WEB1, FakeFarm
from spconnect.cli import app
from spconnect.transport import AuthenticationError

runner = CliRunner()


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        f"SP_BASE_URL={WEB1}\n"
        "SP_AUTH_MODE=anonymous\n"
        "SP_USERNAME=pkober\n"
        "SP_PASSWORD=supersecret\n"
        "SP_ALLOW_LEGACY_TLS=false\n"
        "SP_REQUESTS_PER_SECOND=10000\n"
        "SP_PAGE_SIZE=2\n"
        "SP_DOWNLOAD_FILES=false\n"
        f"SP_LANDING_DIR={tmp_path / 'landing'}\n"
        f"SP_STATE_FILE={tmp_path / 'landing' / '_state.json'}\n"
        "SP_LOG_LEVEL=CRITICAL\n",
        encoding="utf-8",
    )
    return path


def run(env_file: Path, *args: str):
    return runner.invoke(app, ["--env-file", str(env_file), *args])


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "spconnect" in result.stdout


def test_help_lists_every_documented_command() -> None:
    result = runner.invoke(app, ["--help"])
    for command in ("probe", "discover", "schema", "graph", "crawl", "sync", "verify-time", "stats"):
        assert command in result.stdout


def test_probe_succeeds(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "probe")
    assert result.exit_code == 0
    assert "12.0.0.6421" in result.stdout
    assert "WSS 3.0 / MOSS 2007" in result.stdout
    assert "2 readable via GetAllSubWebCollection" in result.stdout
    assert result.stdout.rstrip().endswith("PROBE OK — the connector can read this farm.")


def test_probe_narrates_every_step_in_order(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "probe")
    expected = [
        "Reach the server",
        "Determine authentication scheme",
        "Authenticate",
        "Read server build number",
        "Enumerate webs",
        "List inventory on the first web",
        "SiteData liveness",
        "ListData.svc",
    ]
    positions = [result.stdout.index(label) for label in expected]
    assert positions == sorted(positions)
    assert "[1/8]" in result.stdout and "[8/8]" in result.stdout
    assert result.stdout.count(" OK    ") >= 8
    # This env file runs SP_AUTH_MODE=anonymous. Reporting "login successful"
    # there was a false green: no credential was configured, so none was proven.
    assert "anonymous — no credential configured" in result.stdout
    assert "login successful" not in result.stdout


def test_probe_reports_request_count_and_bytes(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "probe")
    assert "HTTP requests" in result.stdout
    assert "received" in result.stdout


def test_the_request_count_includes_the_diagnostic_round_trips(env_file: Path, farm: FakeFarm) -> None:
    """The auth probe bypasses the counter, so the footer used to under-report it."""
    result = run(env_file, "probe")
    total = int(result.stdout.split(" HTTP requests")[0].rsplit("\n", 1)[-1])
    assert total >= len(farm.mock.calls)


def test_quiet_suppresses_the_narration(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "--quiet", "probe")
    assert result.exit_code == 0
    assert "[1/8]" not in result.stdout


def test_probe_exits_nonzero_when_the_server_is_unreachable(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("SP_BASE_URL=http://127.0.0.1:9\nSP_AUTH_MODE=anonymous\nSP_MAX_RETRIES=1\n", "utf-8")
    result = runner.invoke(app, ["--env-file", str(env), "probe"])
    assert result.exit_code != 0
    assert "FAILED" in result.stdout


def test_discover_writes_webs_json(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    result = run(env_file, "discover")
    assert result.exit_code == 0
    assert "Servicefälle" in result.stdout

    payload = json.loads((tmp_path / "landing" / "webs.json").read_text(encoding="utf-8"))
    assert payload["count"] == 2


def test_discover_warns_about_unique_scopes(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "discover")
    assert "item-level permissions" in result.stdout
    assert "Servicefälle" in result.stdout


def test_dry_run_fetches_no_items(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "--dry-run", "crawl")
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "Estimated requests" in result.stdout
    assert farm.count("GetListItems") == 0


def test_schema_then_graph(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    assert run(env_file, "schema").exit_code == 0
    assert (tmp_path / "landing" / "_graph.mmd").exists()

    result = run(env_file, "graph", "--format", "mermaid")
    assert result.exit_code == 0
    assert result.stdout.startswith("graph LR")
    assert "dangling lookup edge" in result.stdout


def test_graph_json_and_dot(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    run(env_file, "schema")

    as_json = run(env_file, "graph", "--format", "json")
    assert json.loads(as_json.stdout.split("\n\n")[0])["nodes"]

    as_dot = run(env_file, "graph", "--format", "dot")
    assert "digraph lookups" in as_dot.stdout
    assert (tmp_path / "landing" / "_graph.dot").exists()


def test_graph_to_a_file(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    run(env_file, "schema")
    out = tmp_path / "graph.mmd"
    result = run(env_file, "graph", "--out", str(out))
    assert result.exit_code == 0
    assert out.read_text(encoding="utf-8").startswith("graph LR")


def test_graph_without_cached_schemas_tells_you_what_to_run(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "graph")
    assert result.exit_code == 1
    assert "spconnect schema" in result.stdout


def test_bad_graph_format_is_rejected(env_file: Path, farm: FakeFarm) -> None:
    run(env_file, "schema")
    assert run(env_file, "graph", "--format", "svg").exit_code != 0


def test_crawl_then_stats(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    result = run(env_file, "crawl")
    assert result.exit_code == 0
    assert "SUMMARY" in result.stdout
    assert "items written      : 10" in result.stdout

    stats = run(env_file, "stats")
    assert stats.exit_code == 0
    assert "Lists        : 6" in stats.stdout
    assert "Items        : 10" in stats.stdout
    assert "Servicefälle" in stats.stdout


def test_stats_without_a_landing_zone(env_file: Path) -> None:
    result = run(env_file, "stats")
    assert result.exit_code == 1
    assert "No landing zone" in result.stdout


def test_crawl_reports_failures_without_aborting(env_file: Path, farm: FakeFarm) -> None:
    farm.fail_on["GetListItems"] = "soap_fault.xml"
    result = run(env_file, "crawl")
    assert result.exit_code == 0
    assert "lists failed       : 1" in result.stdout
    assert "ERRORS (1)" in result.stdout


def test_verify_time(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "verify-time", "--list", "Servicefälle", "--item", "1")
    assert result.exit_code == 0
    assert "2009-03-14T08:11:00Z" in result.stdout
    assert "DateInUtc" in result.stdout
    assert "compare against what SharePoint shows" in result.stdout


def test_verify_time_on_an_unknown_list_exits_nonzero(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "verify-time", "--list", "Nope", "--item", "1")
    assert result.exit_code == 1
    assert "FAILED" in result.stdout


def test_password_never_reaches_stdout(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    result = run(env_file, "crawl")
    assert "supersecret" not in result.stdout
    assert "supersecret" not in (tmp_path / "landing" / "_manifest.json").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# flag precedence
# --------------------------------------------------------------------------- #


def test_scope_flags_override_the_env_file(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "--include-lists", "Kunden", "discover")
    assert "Servicefälle" not in result.stdout
    assert "Kunden" in result.stdout


def test_landing_dir_flag_moves_the_state_file_with_it(
    env_file: Path, farm: FakeFarm, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "somewhere-else"
    assert run(env_file, "--landing-dir", str(elsewhere), "crawl").exit_code == 0
    assert (elsewhere / "_state.json").exists()
    assert (elsewhere / "_manifest.json").exists()


def test_resume_skips_completed_lists(env_file: Path, farm: FakeFarm) -> None:
    assert run(env_file, "crawl").exit_code == 0
    result = run(env_file, "crawl", "--resume")
    assert "lists skipped      : 6" in result.stdout


def test_sync_records_the_delete(env_file: Path, farm: FakeFarm, tmp_path: Path) -> None:
    run(env_file, "crawl")

    state_path = tmp_path / "landing" / "_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["lists"][CASES]["change_token"] = "1;3;primed"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = run(env_file, "sync")
    assert result.exit_code == 0
    assert "items deleted      : 1" in result.stdout


# --------------------------------------------------------------------------- #
# diagnostics: auth schemes, discovery method, bad response bodies
# --------------------------------------------------------------------------- #


def test_probe_reports_the_offered_auth_schemes(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "probe")
    assert "Determine authentication scheme" in result.stdout


def test_probe_reports_which_discovery_call_was_used(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "probe")
    assert "via GetAllSubWebCollection" in result.stdout


def test_probe_falls_back_and_says_so(env_file: Path, farm: FakeFarm) -> None:
    farm.always_fail["GetAllSubWebCollection"] = "soap_fault_unknown_action.xml"
    result = run(env_file, "probe")
    assert result.exit_code == 0
    assert "via GetWebCollection" in result.stdout
    assert "falling back" in result.stdout
    assert "2 readable" in result.stdout


def test_probe_dumps_an_unusable_response_body_to_disk(
    env_file: Path, farm: FakeFarm, tmp_path: Path
) -> None:
    # Both discovery calls return an FBA login page: nothing to fall back to.
    farm.always_fail["GetAllSubWebCollection"] = "html_login_page.html"
    farm.always_fail["GetWebCollection"] = "html_login_page.html"

    result = run(env_file, "probe")

    assert result.exit_code == 1
    assert "FAILED" in result.stdout
    assert "forms-authentication login page" in result.stdout
    dumped = tmp_path / "landing" / "_last_bad_response.xml"
    assert dumped.exists()
    assert b"login.aspx" in dumped.read_bytes()
    assert str(dumped) in result.stdout


# --------------------------------------------------------------------------- #
# base URL redirects
# --------------------------------------------------------------------------- #


def ntlm_env_file(tmp_path: Path) -> Path:
    """An env file configured the way the farm that prompted this one was."""
    path = tmp_path / "ntlm.env"
    path.write_text(
        f"SP_BASE_URL={WEB1}\n"
        "SP_AUTH_MODE=ntlm\n"
        "SP_USERNAME=pkober\n"
        "SP_PASSWORD=supersecret\n"
        "SP_ALLOW_LEGACY_TLS=false\n"
        "SP_REQUESTS_PER_SECOND=10000\n"
        f"SP_LANDING_DIR={tmp_path / 'landing'}\n"
        f"SP_STATE_FILE={tmp_path / 'landing' / '_state.json'}\n"
        "SP_LOG_LEVEL=CRITICAL\n",
        encoding="utf-8",
    )
    return path


def test_probe_stops_at_the_auth_step_when_the_base_url_redirects(tmp_path: Path, mocked_responses) -> None:
    mocked_responses.add(responses.GET, WEB1, status=302, headers={"Location": "https://sp/sites/service"})

    result = run(ntlm_env_file(tmp_path), "probe")

    assert result.exit_code == 2
    assert "Determine authentication scheme" in result.stdout
    assert "FAILED" in result.stdout
    assert "SP_BASE_URL=https://sp/sites/service" in result.stdout


def test_probe_does_not_suggest_anonymous_for_a_redirecting_farm(tmp_path: Path, mocked_responses) -> None:
    """The original misdiagnosis: 302 < 400, so the probe called it anonymous."""
    mocked_responses.add(responses.GET, WEB1, status=302, headers={"Location": "https://sp/sites/service"})

    result = run(ntlm_env_file(tmp_path), "probe")

    assert "anonymous" not in result.stdout
    # And it stops rather than narrating four more steps against a dead URL.
    assert "Enumerate webs" not in result.stdout
    assert "Read server build number" not in result.stdout


# --------------------------------------------------------------------------- #
# permissions
# --------------------------------------------------------------------------- #


def test_permissions_reports_declared_and_effective_access(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "permissions")

    assert result.exit_code == 0
    assert "Servicefälle Besucher" in result.stdout  # declared groups
    assert "Lesen" in result.stdout  # declared permission level
    assert "lists readable" in result.stdout  # effective access
    assert "Everything discovered is readable" in result.stdout


def test_permissions_names_what_a_crawl_would_silently_omit(env_file: Path, farm: FakeFarm) -> None:
    farm.always_fail["GetListItems"] = "usergroup_denied.xml"

    result = run(env_file, "permissions")

    assert result.exit_code == 0
    assert "DENIED" in result.stdout
    assert "would silently omit them" in result.stdout


def test_permissions_says_so_when_the_server_will_not_disclose_them(env_file: Path, farm: FakeFarm) -> None:
    farm.always_fail["GetGroupCollectionFromUser"] = "usergroup_denied.xml"
    farm.always_fail["GetRoleCollectionFromUser"] = "usergroup_denied.xml"

    result = run(env_file, "permissions")

    assert result.exit_code == 0
    assert "not permitted to say" in result.stdout
    # The effective half needs no privilege, so it still answers.
    assert "lists readable" in result.stdout


def test_permissions_flags_item_level_scopes(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "permissions")
    assert "[unique-scopes]" in result.stdout
    assert "item-level permissions" in result.stdout


def test_permissions_json_is_machine_readable(env_file: Path, farm: FakeFarm) -> None:
    result = run(env_file, "permissions", "--json")
    payload = json.loads(result.stdout)
    assert payload["complete"] is True
    assert payload["declared"]["roles"] == ["Lesen"]
    assert all("lists" in web for web in payload["webs"])


def test_permissions_stops_when_the_base_url_redirects(tmp_path: Path, mocked_responses) -> None:
    """Same clean exit as probe: one request, and the value to set."""
    mocked_responses.add(responses.GET, WEB1, status=302, headers={"Location": "https://sp/sites/service"})

    result = run(ntlm_env_file(tmp_path), "permissions")

    assert result.exit_code == 2
    assert "SP_BASE_URL=https://sp/sites/service" in result.stdout
    # And it stops before spending an authenticated request on a dead URL.
    assert "Enumerate webs" not in result.stdout


def test_permissions_emits_the_same_auth_evidence_as_probe(env_file: Path, farm: FakeFarm) -> None:
    """The differential used to exist only in probe; the evidence is the same evidence."""
    farm.fail_on["GetAllSubWebCollection"] = AuthenticationError("HTTP 401 for Webs.asmx")

    result = run(env_file, "permissions")

    assert result.exit_code == 2
    assert "AUTH FAILED" in result.stdout
    assert "Differential check" in result.stdout
    assert "Permissions cannot be assessed until the login works." in result.stdout


def test_probe_fails_at_login_when_sharepoint_denies_access(tmp_path: Path, mocked_responses) -> None:
    """The exact shape of the reported farm: step 3 passed, step 5 died on a 401.

    SharePoint answers an authenticated-but-unauthorised browser request by
    redirecting to a page that returns 200 and carries the version header, so
    the login step passed and the first request with no page to redirect to —
    the SOAP call — failed instead, three steps from the cause.
    """
    denied = f"{WEB1}/_layouts/AccessDenied.aspx?Source=%2F"
    env = tmp_path / "basic.env"
    env.write_text(
        f"SP_BASE_URL={WEB1}\nSP_AUTH_MODE=basic\nSP_USERNAME=pkober\nSP_PASSWORD=x\n"
        f"SP_REQUESTS_PER_SECOND=10000\nSP_LANDING_DIR={tmp_path / 'l'}\n"
        f"SP_STATE_FILE={tmp_path / 'l' / 's.json'}\nSP_LOG_LEVEL=CRITICAL\n",
        encoding="utf-8",
    )
    mocked_responses.add(responses.GET, WEB1, status=401, headers={"WWW-Authenticate": "Basic"})
    mocked_responses.add(responses.HEAD, WEB1, status=302, headers={"Location": denied})
    mocked_responses.add(
        responses.HEAD,
        denied,
        status=200,
        headers={"MicrosoftSharePointTeamServices": "14.0.0.7149"},
    )
    mocked_responses.add(responses.GET, WEB1, status=302, headers={"Location": denied})
    mocked_responses.add(responses.GET, f"{WEB1}/_vti_bin/Webs.asmx", status=401)

    result = run(env, "probe")

    assert result.exit_code == 2
    assert "SharePoint sent us to" in result.stdout
    assert "permissions problem" in result.stdout
    # It must not sail past this into the steps that depend on a working login.
    assert "Enumerate webs" not in result.stdout
    # And the differential must not read that denial redirect as reaching the site.
    assert "cannot read this web at all" in result.stdout
