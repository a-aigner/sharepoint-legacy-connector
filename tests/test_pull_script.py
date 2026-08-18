"""Tests for ``scripts/pull_tickets_and_comments.py``.

The script is not part of the package, so it is loaded by path. It has to be
registered in ``sys.modules`` before execution because it defines a dataclass,
and ``@dataclass`` resolves its module through ``sys.modules``.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import responses

from spconnect.config import Settings
from spconnect.services.odata import ODataService
from spconnect.transport import Transport

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "pull_tickets_and_comments.py"
BASE = "http://sp"
TICKET_URL = re.compile(r"http://sp/_vti_bin/ListData\.svc/Ticket(\?.*)?$")


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("pull_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pull_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def pull_script() -> Any:
    return load_script()


@pytest.fixture
def transport() -> Transport:
    return Transport(Settings(base_url=BASE, auth_mode="anonymous", max_retries=1))


@pytest.fixture
def service(transport: Transport) -> ODataService:
    return ODataService(transport, BASE)


def row(ident: int) -> dict[str, Any]:
    """A ticket row shaped like the real farm: scalars plus navigation stubs."""
    return {
        "Id": ident,
        "Title": f"T{ident}",
        "Created": "/Date(1320399285000)/",
        "AssignedTo": {"__deferred": {"uri": f"{BASE}/x/AssignedTo"}},
        "CreatedBy": {"__deferred": {"uri": f"{BASE}/x/CreatedBy"}},
    }


def feed(rows: list[dict[str, Any]]) -> str:
    return json.dumps({"d": {"results": rows}})


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #


@responses.activate
def test_schema_discovery_lists_navigation_stubs(pull_script: Any, transport: Transport, service: ODataService) -> None:
    """The stubs are what $expand can fill in — the schema has to name them."""
    responses.add(responses.GET, TICKET_URL, body=feed([row(1)]), content_type="application/json")

    schema = pull_script.discover_schema(transport, service, "Ticket")

    assert schema.key == "Id"
    assert sorted(schema.deferred) == ["AssignedTo", "CreatedBy"]


# --------------------------------------------------------------------------- #
# expansion
# --------------------------------------------------------------------------- #


@responses.activate
def test_pull_requests_the_expansions_it_was_given(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    responses.add(responses.GET, TICKET_URL, body=feed([row(1)]), content_type="application/json")
    responses.add(responses.GET, TICKET_URL, body=feed([]), content_type="application/json")

    schema = pull_script.CollectionSchema(
        entity_set="Ticket", properties=["Id"], key="Id", created="Created",
        deferred=["AssignedTo", "CreatedBy"],
    )
    pull_script.pull(
        transport, service, schema, tmp_path / "t.jsonl",
        page_size=10, limit=None, skip_count=True, expand=["AssignedTo", "CreatedBy"],
    )

    assert any("$expand=AssignedTo,CreatedBy" in call.request.url for call in responses.calls)


@responses.activate
def test_pull_falls_back_when_the_server_refuses_the_expansion(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    """A refused $expand must cost the expansion, not the extraction."""

    def dispatch(request: Any) -> tuple[int, dict[str, str], str]:
        if "$expand" in (request.url or ""):
            return 400, {}, json.dumps({"error": {"message": {"value": "expand refused"}}})
        return 200, {"Content-Type": "application/json"}, feed([row(1)] if "gt%200" in request.url else [])

    responses.add_callback(responses.GET, TICKET_URL, callback=dispatch, content_type="application/json")

    schema = pull_script.CollectionSchema(
        entity_set="Ticket", properties=["Id"], key="Id", created="Created", deferred=["AssignedTo"],
    )
    written = pull_script.pull(
        transport, service, schema, tmp_path / "t.jsonl",
        page_size=10, limit=None, skip_count=True, expand=["AssignedTo"],
    )

    assert written == 1, "rows must still be extracted after the expansion is refused"
    assert (tmp_path / "t.jsonl").exists()


# --------------------------------------------------------------------------- #
# choosing what to expand
# --------------------------------------------------------------------------- #


def schema_with(deferred: list[str], pull_script: Any) -> Any:
    return pull_script.CollectionSchema(
        entity_set="Ticket", properties=["Id"], key="Id", created="Created", deferred=deferred
    )


def test_auto_expands_exactly_the_stubs_that_exist(pull_script: Any) -> None:
    schema = schema_with(["AssignedTo", "CreatedBy"], pull_script)
    assert pull_script.resolve_expansions(schema, "auto") == (["AssignedTo", "CreatedBy"], [])


def test_unknown_expansion_names_are_dropped_not_sent(pull_script: Any) -> None:
    """Asking for a property the entity does not have earns a 400 for the whole
    page, so an unknown name must never reach the query."""
    schema = schema_with(["AssignedTo"], pull_script)
    wanted, unknown = pull_script.resolve_expansions(schema, "AssignedTo,Nonexistent")
    assert wanted == ["AssignedTo"]
    assert unknown == ["Nonexistent"]


def test_no_expansion_requested_means_none_sent(pull_script: Any) -> None:
    schema = schema_with(["AssignedTo"], pull_script)
    assert pull_script.resolve_expansions(schema, None) == ([], [])


# --------------------------------------------------------------------------- #
# a page too large for the server to answer in time
# --------------------------------------------------------------------------- #


def size_limited_farm(max_top: int) -> Any:
    """Answers only pages of at most ``max_top`` rows; larger ones never complete.

    A read timeout and an exhausted 5xx retry arrive identically at this layer —
    the request did not complete — so a 500 stands in for the farm's 120-second
    ReadTimeout without making the test wait.
    """
    rows = [row(i) for i in range(1, 13)]

    def dispatch(request: Any) -> tuple[int, dict[str, str], str]:
        url = request.url or ""
        top_match = re.search(r"\$top=(\d+)", url)
        top = int(top_match.group(1)) if top_match else 1000   # server-driven default
        if top > max_top:
            return 500, {}, "timed out"
        after_match = re.search(r"gt%20(\d+)", url)
        after = int(after_match.group(1)) if after_match else 0
        page = [r for r in rows if r["Id"] > after][:top]
        return 200, {"Content-Type": "application/json"}, feed(page)

    return dispatch


@responses.activate
def test_page_size_halves_until_the_server_can_answer(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    responses.add_callback(responses.GET, TICKET_URL, callback=size_limited_farm(100),
                           content_type="application/json")
    schema = pull_script.CollectionSchema(
        entity_set="Ticket", properties=["Id"], key="Id", created="Created"
    )

    written = pull_script.pull(
        transport, service, schema, tmp_path / "t.jsonl",
        page_size=400, limit=None, skip_count=True,
    )

    assert written == 12, "every row must still arrive after the page size is reduced"
    tops = {int(m.group(1)) for c in responses.calls
            if (m := re.search(r"\$top=(\d+)", c.request.url))}
    assert 400 in tops and 100 in tops, f"expected 400 to be reduced toward 100, saw {sorted(tops)}"


@responses.activate
def test_page_size_reduction_stops_instead_of_looping_forever(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    """A server that answers nothing must end the run, not shrink indefinitely."""
    responses.add_callback(responses.GET, TICKET_URL, callback=size_limited_farm(0),
                           content_type="application/json")
    schema = pull_script.CollectionSchema(
        entity_set="Ticket", properties=["Id"], key="Id", created="Created"
    )

    with pytest.raises(pull_script.PageUnavailable):
        pull_script.pull(
            transport, service, schema, tmp_path / "t.jsonl",
            page_size=400, limit=None, skip_count=True,
        )

    tops = {int(m.group(1)) for c in responses.calls
            if (m := re.search(r"\$top=(\d+)", c.request.url))}
    assert min(tops) >= pull_script.MIN_PAGE_SIZE, f"went below the floor: {sorted(tops)}"
    assert len(tops) <= 8, f"halving should terminate quickly, saw {sorted(tops)}"


# --------------------------------------------------------------------------- #
# the user list
# --------------------------------------------------------------------------- #


def test_optional_entity_set_resolves_when_present(pull_script: Any, service: ODataService) -> None:
    available = ["Ticket", "TicketComment", "UserInformationList"]
    assert pull_script.resolve_optional_entity_set(service, "UserInformationList", available) == (
        "UserInformationList"
    )


def test_optional_entity_set_is_case_insensitive(pull_script: Any, service: ODataService) -> None:
    available = ["Ticket", "userinformationlist"]
    assert pull_script.resolve_optional_entity_set(service, "UserInformationList", available) == (
        "userinformationlist"
    )


def test_optional_entity_set_returns_none_rather_than_exiting(
    pull_script: Any, service: ODataService
) -> None:
    """A farm without the list must cost the author names, not the extraction.

    resolve_entity_set raises SystemExit, which is right for the ticket list and
    wrong for an optional one.
    """
    assert pull_script.resolve_optional_entity_set(service, "UserInformationList", ["Ticket"]) is None


@responses.activate
def test_pull_users_writes_the_directory_when_the_list_exists(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    users = re.compile(r"http://sp/_vti_bin/ListData\.svc/UserInformationList(\?.*)?$")
    responses.add(responses.GET, users, content_type="application/json",
                  body=json.dumps({"d": {"results": [{"Id": 13, "Name": "A. Schoene"}]}}))
    responses.add(responses.GET, users, content_type="application/json",
                  body=json.dumps({"d": {"results": [{"Id": 13, "Name": "A. Schoene"}]}}))
    responses.add(responses.GET, users, body=feed([]), content_type="application/json")

    out = tmp_path / "users.jsonl"
    written = pull_script.pull_users(
        transport, service, ["Ticket", "UserInformationList"], out, page_size=100
    )

    assert written == 1
    assert json.loads(out.read_text(encoding="utf-8").splitlines()[0])["Name"] == "A. Schoene"


@responses.activate
def test_pull_users_is_skipped_when_the_list_is_absent(
    pull_script: Any, transport: Transport, service: ODataService, tmp_path: Path
) -> None:
    out = tmp_path / "users.jsonl"

    written = pull_script.pull_users(transport, service, ["Ticket"], out, page_size=100)

    assert written is None
    assert not out.exists(), "no collection means no file, not an empty one"
    assert not responses.calls, "nothing should be requested for a collection that is not there"


# --------------------------------------------------------------------------- #
# attachments
# --------------------------------------------------------------------------- #


def test_attachment_media_url_uses_the_composite_key(pull_script: Any) -> None:
    """AttachmentsItem is keyed on (EntitySet, ItemId, Name), not on an Id."""
    media, direct = pull_script.attachment_urls(BASE, "Ticket", 3, "report.pdf")
    assert media == (
        "http://sp/_vti_bin/ListData.svc/Attachments"
        "(EntitySet='Ticket',ItemId=3,Name='report.pdf')/$value"
    )
    assert direct == "http://sp/Lists/Ticket/Attachments/3/report.pdf"


def test_attachment_url_doubles_apostrophes_in_the_odata_literal(pull_script: Any) -> None:
    """A single quote ends an OData string literal; doubling escapes it. Without
    this an O'Brien attachment produces a malformed key, not a 404."""
    media, _ = pull_script.attachment_urls(BASE, "Ticket", 7, "O'Brien.pdf")
    assert "Name='O''Brien.pdf'" in media


def test_attachment_url_percent_encodes_spaces_and_umlauts(pull_script: Any) -> None:
    media, direct = pull_script.attachment_urls(BASE, "Ticket", 9, "Prüfbericht Saal 2.pdf")
    assert " " not in media and " " not in direct
    assert "%C3%BC" in direct or "%FC" in direct


def test_looks_like_archive_flags_containers_worth_unpacking(pull_script: Any) -> None:
    assert pull_script.file_kind("logs.zip") == "archive"
    assert pull_script.file_kind("bericht.PDF") == "document"
    assert pull_script.file_kind("screenshot.png") == "image"
    assert pull_script.file_kind("dump.log") == "text"
    assert pull_script.file_kind("weird.xyz") == "other"


ATTACH_URL = re.compile(r"http://sp/.*")


@responses.activate
def test_fetch_attachment_reports_the_form_that_worked(
    pull_script: Any, transport: Transport, tmp_path: Path
) -> None:
    """Which URL form a build serves is not knowable in advance, so both are
    tried and the working one is named."""
    def dispatch(request: Any) -> tuple[int, dict[str, str], bytes]:
        if "$value" in (request.url or ""):
            return 404, {}, b""
        return 200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 hello"

    responses.add_callback(responses.GET, ATTACH_URL, callback=dispatch)

    result = pull_script.fetch_attachment(transport, BASE, "Ticket", 3, "report.pdf", save_to=None)

    assert result["ok"] is True
    assert result["via"] == "direct"
    assert result["bytes"] == 14
    assert result["content_type"] == "application/pdf"


@responses.activate
def test_fetch_attachment_reports_failure_of_both_forms(
    pull_script: Any, transport: Transport
) -> None:
    responses.add_callback(responses.GET, ATTACH_URL, callback=lambda r: (404, {}, b""))

    result = pull_script.fetch_attachment(transport, BASE, "Ticket", 3, "gone.pdf", save_to=None)

    assert result["ok"] is False
    assert result["via"] is None


@responses.activate
def test_fetch_attachment_saves_the_bytes_when_asked(
    pull_script: Any, transport: Transport, tmp_path: Path
) -> None:
    responses.add_callback(
        responses.GET, ATTACH_URL,
        callback=lambda r: (200, {"Content-Type": "application/zip"}, b"PK\x03\x04payload"),
    )

    result = pull_script.fetch_attachment(
        transport, BASE, "Ticket", 42, "logs.zip", save_to=tmp_path
    )

    saved = tmp_path / "42__logs.zip"
    assert saved.read_bytes() == b"PK\x03\x04payload"
    assert result["kind"] == "archive"


# --------------------------------------------------------------------------- #
# expansion is per collection
# --------------------------------------------------------------------------- #


def test_expand_applies_to_tickets_and_leaves_comments_alone(pull_script: Any) -> None:
    """The measured reason: tickets expanded at 42-53 rows/s and finished, the
    same expansion on comments ran at 5 rows/s and timed out. One flag driving
    both collections cannot express that."""
    tickets = schema_with(["AssignedTo", "CreatedBy"], pull_script)
    comments = schema_with(["CreatedBy", "EmailReceiver"], pull_script)

    plan = pull_script.expansion_plan(tickets, comments, tickets_spec="auto", comments_spec=None)

    assert plan.tickets == ["AssignedTo", "CreatedBy"]
    assert plan.comments == []


def test_comments_can_still_be_expanded_when_asked_explicitly(pull_script: Any) -> None:
    tickets = schema_with(["AssignedTo"], pull_script)
    comments = schema_with(["CreatedBy", "EmailReceiver"], pull_script)

    plan = pull_script.expansion_plan(
        tickets, comments, tickets_spec=None, comments_spec="EmailReceiver"
    )

    assert plan.tickets == []
    assert plan.comments == ["EmailReceiver"]


def test_expansion_plan_collects_unknown_names_from_both(pull_script: Any) -> None:
    tickets = schema_with(["AssignedTo"], pull_script)
    comments = schema_with(["CreatedBy"], pull_script)

    plan = pull_script.expansion_plan(
        tickets, comments, tickets_spec="AssignedTo,Nope", comments_spec="AlsoNope"
    )

    assert sorted(plan.unknown) == ["AlsoNope", "Nope"]


# --------------------------------------------------------------------------- #
# attachments reached through their item
# --------------------------------------------------------------------------- #


def test_attachment_navigation_url_goes_through_the_item(pull_script: Any, service: ODataService) -> None:
    """The standalone Attachments set is not enumerable; the navigation from an
    item is. An empty collection query means 'ask differently', not 'none exist'."""
    assert pull_script.attachment_nav_url(service, "Ticket", 3) == (
        "http://sp/_vti_bin/ListData.svc/Ticket(3)/Attachments"
    )


@responses.activate
def test_attachments_via_navigation_collects_across_items(
    pull_script: Any, transport: Transport, service: ODataService
) -> None:
    def dispatch(request: Any) -> tuple[int, dict[str, str], str]:
        item = int(re.search(r"Ticket\((\d+)\)", request.url).group(1))
        if item == 2:
            return 200, {"Content-Type": "application/json"}, json.dumps({"d": {"results": []}})
        return 200, {"Content-Type": "application/json"}, json.dumps(
            {"d": {"results": [{"EntitySet": "Ticket", "ItemId": item, "Name": f"f{item}.pdf"}]}}
        )

    responses.add_callback(
        responses.GET, re.compile(r"http://sp/_vti_bin/ListData\.svc/Ticket\(\d+\)/Attachments"),
        callback=dispatch, content_type="application/json",
    )

    found = pull_script.attachments_via_navigation(transport, service, "Ticket", [1, 2, 3])

    assert [r["Name"] for r in found] == ["f1.pdf", "f3.pdf"]


@responses.activate
def test_attachments_via_navigation_survives_a_failing_item(
    pull_script: Any, transport: Transport, service: ODataService
) -> None:
    """One unreadable ticket must not end the survey."""
    def dispatch(request: Any) -> tuple[int, dict[str, str], str]:
        if "Ticket(2)" in (request.url or ""):
            return 500, {}, "boom"
        return 200, {"Content-Type": "application/json"}, json.dumps(
            {"d": {"results": [{"EntitySet": "Ticket", "ItemId": 1, "Name": "ok.pdf"}]}}
        )

    responses.add_callback(
        responses.GET, re.compile(r"http://sp/_vti_bin/ListData\.svc/Ticket\(\d+\)/Attachments"),
        callback=dispatch, content_type="application/json",
    )

    found = pull_script.attachments_via_navigation(transport, service, "Ticket", [1, 2])

    assert [r["Name"] for r in found] == ["ok.pdf"]


@responses.activate
def test_fetch_attachment_rejects_an_odata_envelope_posing_as_a_file(
    pull_script: Any, transport: Transport
) -> None:
    """A 200 is not proof of a file. SharePoint answers an unsupported media
    request with an OData envelope, and a sign-in redirect lands as HTML —
    both would otherwise be reported as the attachment."""
    def dispatch(request: Any) -> tuple[int, dict[str, str], bytes]:
        if "$value" in (request.url or ""):
            return 200, {"Content-Type": "application/json"}, b'{"d": {"results": []}}'
        return 200, {"Content-Type": "application/pdf"}, b"%PDF-1.4 real"

    responses.add_callback(responses.GET, ATTACH_URL, callback=dispatch)

    result = pull_script.fetch_attachment(transport, BASE, "Ticket", 3, "x.pdf", save_to=None)

    assert result["via"] == "direct", "the JSON envelope must not be accepted as the file"
    assert result["bytes"] == 13
