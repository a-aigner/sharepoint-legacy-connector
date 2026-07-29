"""``Lists.asmx``: discovery, schema, attachments, change tokens."""

from __future__ import annotations

import pytest

from conftest import CASES, DOKUMENTE, KUNDEN, WEB1, WEB2, FakeFarm, make_settings
from spconnect.crawl import Crawler
from spconnect.services.lists import SYSTEM_LIST_TITLES, ListsService, is_system_list
from spconnect.transport import Transport


@pytest.fixture
def service(farm: FakeFarm, transport: Transport) -> ListsService:
    return ListsService(transport, WEB1)


# --------------------------------------------------------------------------- #
# GetListCollection
# --------------------------------------------------------------------------- #


def test_get_list_collection_parses_every_list(service: ListsService) -> None:
    lists = service.get_list_collection()
    assert [li.title for li in lists] == [
        "Servicefälle",
        "Kunden",
        "Dokumente",
        "Master Page Gallery",
        "Konfiguration",
    ]


def test_list_attributes(service: ListsService) -> None:
    cases = next(li for li in service.get_list_collection() if li.guid == CASES)
    assert cases.item_count == 3
    assert cases.base_type == "0"
    assert cases.server_template == "100"
    assert cases.enable_attachments is True
    assert cases.has_unique_scopes is True
    assert cases.root_folder == "/sites/service/Lists/Cases"
    assert cases.web_url == WEB1
    assert cases.created == "20050112 09:14:22"


def test_base_type_names(service: ListsService) -> None:
    by_guid = {li.guid: li for li in service.get_list_collection()}
    assert by_guid[CASES].base_type_name == "generic_list"
    assert by_guid[DOKUMENTE].is_document_library


def test_system_lists_are_recognised() -> None:
    assert "Workflow History" in SYSTEM_LIST_TITLES
    assert "Master Page Gallery" in SYSTEM_LIST_TITLES


# --------------------------------------------------------------------------- #
# scope filtering (the policy lives on the crawler)
# --------------------------------------------------------------------------- #


def _crawler(tmp_path, transport: Transport, **overrides) -> Crawler:
    return Crawler(make_settings(tmp_path, **overrides), transport)


def test_system_and_hidden_lists_are_filtered_by_default(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport)
    _webs, by_web = crawler.discover()
    assert [li.title for li in by_web[WEB1]] == ["Servicefälle", "Kunden", "Dokumente"]


def test_hidden_lists_can_be_opted_in(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, include_hidden_lists=True)
    _webs, by_web = crawler.discover()
    assert "Konfiguration" in [li.title for li in by_web[WEB1]]
    # The system-list block list still applies.
    assert "Master Page Gallery" not in [li.title for li in by_web[WEB1]]


def test_document_libraries_can_be_excluded(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, include_document_libraries=False)
    _webs, by_web = crawler.discover()
    assert [li.title for li in by_web[WEB1]] == ["Servicefälle", "Kunden"]


def test_include_lists_narrows_the_scope(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, include_lists="Servicefälle")
    _webs, by_web = crawler.discover()
    assert [li.title for li in by_web[WEB1]] == ["Servicefälle"]
    assert [li.title for li in by_web[WEB2]] == ["Servicefälle 2008"]


def test_exclude_lists_wins(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, exclude_lists="Dokumente,Kunden")
    _webs, by_web = crawler.discover()
    assert [li.title for li in by_web[WEB1]] == ["Servicefälle"]


def test_include_webs_narrows_the_scope(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, include_webs="cases2008")
    webs, by_web = crawler.discover()
    assert [w.url for w in webs] == [WEB2]
    assert set(by_web) == {WEB2}


def test_exclude_webs(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport, exclude_webs="cases2008")
    webs, _by_web = crawler.discover()
    assert [w.url for w in webs] == [WEB1]


def test_unique_scopes_are_reported(tmp_path, farm: FakeFarm, transport) -> None:
    crawler = _crawler(tmp_path, transport)
    crawler.discover()
    assert crawler.report.unique_scope_lists == [f"{WEB1} :: Servicefälle"]


# --------------------------------------------------------------------------- #
# GetList
# --------------------------------------------------------------------------- #


def test_get_list_schema_merges_discovery_metadata(service: ListsService) -> None:
    info = next(li for li in service.get_list_collection() if li.guid == CASES)
    schema = service.get_list_schema(info)
    assert schema.list_info.guid == CASES
    assert schema.list_info.web_url == WEB1
    assert schema.list_info.item_count == 3
    assert len(schema.fields) > 20
    assert schema.field_map()["Kunde"].lookup_list == KUNDEN


def test_get_list_uses_the_guid_not_the_title(service: ListsService, farm: FakeFarm) -> None:
    info = next(li for li in service.get_list_collection() if li.guid == CASES)
    service.get_list_schema(info)
    request = next(r for r in farm.requests if r.operation == "GetList")
    assert request.param("listName") == CASES


def test_is_system_list(service: ListsService) -> None:
    lists = {li.title: li for li in service.get_list_collection()}
    assert is_system_list(lists["Master Page Gallery"])
    assert not is_system_list(lists["Servicefälle"])


# --------------------------------------------------------------------------- #
# GetAttachmentCollection
# --------------------------------------------------------------------------- #


def test_get_attachment_collection(service: ListsService) -> None:
    urls = service.get_attachment_collection(CASES, 3)
    assert urls == [
        "http://sp/sites/service/Lists/Cases/Attachments/3/Prüfprotokoll.pdf",
        "http://sp/sites/service/Lists/Cases/Attachments/3/messwerte.csv",
    ]


def test_attachment_collection_sends_the_item_id(service: ListsService, farm: FakeFarm) -> None:
    service.get_attachment_collection(CASES, 3)
    request = farm.requests[-1]
    assert request.param("listItemID") == "3"


# --------------------------------------------------------------------------- #
# GetListItemChangesSinceToken
# --------------------------------------------------------------------------- #


def test_change_batch_parses_updates_deletes_and_the_new_token(service: ListsService) -> None:
    batch = service.get_list_item_changes_since_token(CASES, change_token="1;3;old")
    assert [r["ows_ID"] for r in batch.rows] == ["1"]
    assert batch.deleted_ids == [2]
    assert batch.last_change_token.startswith("1;3;11111111")
    assert batch.more_changes is False
    assert batch.invalid_token is False


def test_change_token_is_sent_after_the_query_options(service: ListsService, farm: FakeFarm) -> None:
    service.get_list_item_changes_since_token(CASES, change_token="1;3;old")
    request = farm.requests[-1]
    assert request.param("changeToken") == "1;3;old"
    children = [c.tag.rsplit("}", 1)[-1] for c in request.root.iter()]
    assert children.index("queryOptions") < children.index("changeToken")


def test_first_call_omits_the_token(service: ListsService, farm: FakeFarm) -> None:
    service.get_list_item_changes_since_token(CASES, change_token=None)
    assert farm.requests[-1].param("changeToken") is None


def test_invalid_token_is_detected(service: ListsService, farm: FakeFarm) -> None:
    farm.changes_fixture = "lists_getlistitemchangessincetoken_invalid.xml"
    batch = service.get_list_item_changes_since_token(CASES, change_token="stale")
    assert batch.invalid_token is True
    assert batch.rows == []
