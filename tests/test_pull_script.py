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
