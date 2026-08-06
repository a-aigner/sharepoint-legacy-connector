"""What the credential can read — declared by the server, and proven by trying."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import WEB1, WEB2, FakeFarm, make_settings
from spconnect.models import WebRef
from spconnect.permissions import probe_access
from spconnect.services.usergroup import UserGroupService, login_variants
from spconnect.transport import AuthenticationError, Transport


@pytest.fixture
def tp(tmp_path: Path) -> Transport:
    return Transport(make_settings(tmp_path))


WEBS = [WebRef(title="Service", url=WEB1), WebRef(title="Cases 2008", url=WEB2)]


# --------------------------------------------------------------------------- #
# declared — UserGroup.asmx
# --------------------------------------------------------------------------- #


def test_declared_groups_and_roles_are_read(tp: Transport, farm: FakeFarm) -> None:
    result = UserGroupService(tp, WEB1).describe("CONTOSO\\pkober")

    assert result.groups == ["Servicefälle Besucher", "CRM Leser"]
    assert result.roles == ["Lesen"]
    assert result.known is True


def test_being_refused_the_answer_is_not_the_same_as_having_nothing(tp: Transport, farm: FakeFarm) -> None:
    """Enumerating permissions is itself privileged; a read-only account is often denied."""
    farm.always_fail["GetGroupCollectionFromUser"] = "usergroup_denied.xml"
    farm.always_fail["GetRoleCollectionFromUser"] = "usergroup_denied.xml"

    result = UserGroupService(tp, WEB1).describe("CONTOSO\\pkober")

    assert result.known is False
    assert result.groups == [] and result.roles == []
    assert any("Zugriff verweigert" in reason for reason in result.unavailable)


def test_the_same_refusal_is_not_reported_once_per_login_form(tp: Transport, farm: FakeFarm) -> None:
    farm.always_fail["GetGroupCollectionFromUser"] = "usergroup_denied.xml"
    farm.always_fail["GetRoleCollectionFromUser"] = "usergroup_denied.xml"

    result = UserGroupService(tp, WEB1).describe("pkober")

    # Two login forms x two operations, but only two distinct complaints.
    assert len(result.unavailable) == len(set(result.unavailable)) == 2


def test_an_empty_username_is_reported_rather_than_queried(tp: Transport, farm: FakeFarm) -> None:
    result = UserGroupService(tp, WEB1).describe("")
    assert result.known is False
    assert farm.count("GetGroupCollectionFromUser") == 0


@pytest.mark.parametrize(
    ("username", "expected"),
    [
        ("CONTOSO\\svc", ["CONTOSO\\svc", "i:0#.w|CONTOSO\\svc"]),
        ("i:0#.w|CONTOSO\\svc", ["i:0#.w|CONTOSO\\svc"]),
        ("", []),
        ("  ", []),
    ],
)
def test_login_variants_cover_classic_and_claims(username: str, expected: list[str]) -> None:
    assert login_variants(username) == expected


# --------------------------------------------------------------------------- #
# effective — try the reads the crawler would
# --------------------------------------------------------------------------- #


def test_everything_readable_reports_complete(tp: Transport, farm: FakeFarm) -> None:
    report = probe_access(tp, WEBS)

    assert report.complete is True
    assert len(report.readable_webs) == 2
    assert report.readable_lists == report.total_lists > 0


def test_an_unreadable_list_is_named_with_its_reason(tp: Transport, farm: FakeFarm) -> None:
    farm.always_fail["GetListItems"] = "usergroup_denied.xml"

    report = probe_access(tp, WEBS)

    assert report.complete is False
    assert report.readable_lists == 0
    denied = [entry for web in report.webs for entry in web.denied_lists]
    assert denied and all("Zugriff verweigert" in (e.reason or "") for e in denied)


def test_an_unreadable_web_does_not_stop_the_rest(tp: Transport, farm: FakeFarm) -> None:
    farm.always_fail["GetListCollection"] = "usergroup_denied.xml"

    report = probe_access(tp, WEBS)

    assert len(report.denied_webs) == 2
    assert report.total_lists == 0
    assert all(web.reason for web in report.denied_webs)


def test_probing_items_can_be_skipped(tp: Transport, farm: FakeFarm) -> None:
    report = probe_access(tp, WEBS, probe_items=False)

    assert farm.count("GetListItems") == 0
    assert report.readable_lists == report.total_lists


def test_a_broken_login_is_not_reported_as_a_permissions_finding(tp: Transport, farm: FakeFarm) -> None:
    """401 means the session is unusable; "nothing is readable" would mislead."""
    farm.fail_on["GetListCollection"] = AuthenticationError("HTTP 401")

    with pytest.raises(AuthenticationError):
        probe_access(tp, WEBS)


def test_item_level_permissions_are_called_out(tp: Transport, farm: FakeFarm) -> None:
    """Readable does not mean complete when a list has unique scopes."""
    report = probe_access(tp, WEBS)

    scoped = report.unique_scope_lists
    assert scoped, "the fixture farm has a list with broken inheritance"
    assert all(entry.has_unique_scopes for entry in scoped)
