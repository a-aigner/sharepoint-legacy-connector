"""ID-based paging: termination, resumption, and the runaway guard.

Paging is on the ``ID`` counter rather than ``ListItemCollectionPositionNext``
because the token is not resumable across process restarts. That choice is only
safe if the loop provably terminates, which is what this file checks.
"""

from __future__ import annotations

import pytest

from conftest import CASES, KUNDEN, WEB1, FakeFarm, make_settings
from spconnect.crawl import Crawler
from spconnect.services.lists import ListsService
from spconnect.transport import Transport


@pytest.fixture
def service(farm: FakeFarm, transport: Transport) -> ListsService:
    return ListsService(transport, WEB1)


def _crawler(tmp_path, transport: Transport, **overrides) -> Crawler:
    return Crawler(make_settings(tmp_path, **overrides), transport)


def _schema(crawler: Crawler, guid: str):
    _webs, by_web = crawler.discover()
    for lists in by_web.values():
        for info in lists:
            if info.guid == guid:
                return crawler.lists_service(info.web_url).get_list_schema(info)
    raise AssertionError(f"list {guid} not discovered")


# --------------------------------------------------------------------------- #
# request shape
# --------------------------------------------------------------------------- #


def test_first_page_starts_at_id_zero(service: ListsService, farm: FakeFarm) -> None:
    service.get_list_items(CASES, last_id=0, row_limit=2)
    request = farm.requests[-1]
    assert request.last_id == 0
    assert request.row_limit == 2


def test_subsequent_pages_send_the_last_seen_id(service: ListsService, farm: FakeFarm) -> None:
    service.get_list_items(CASES, last_id=2, row_limit=2)
    assert farm.requests[-1].last_id == 2


def test_page_exposes_the_max_id(service: ListsService) -> None:
    page = service.get_list_items(CASES, last_id=0, row_limit=2)
    assert [r["ows_ID"] for r in page.rows] == ["1", "2"]
    assert page.max_id == 2
    assert page.item_count == 2


def test_max_id_of_an_empty_page_is_none(service: ListsService) -> None:
    page = service.get_list_items(KUNDEN, last_id=0, row_limit=2)
    assert page.rows == []
    assert page.max_id is None


def test_max_id_tolerates_lookup_encoded_ids(service: ListsService) -> None:
    page = service.get_list_items(CASES, last_id=0, row_limit=2)
    page.rows.append({"ows_ID": "9;#9"})
    assert page.max_id == 9


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


def test_loop_terminates_on_a_short_page(tmp_path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, CASES)
    written = crawler.crawl_list(schema.list_info, schema)

    # 3 items at page size 2 => a full page, then a short page, then stop.
    assert written == 3
    item_calls = [r for r in farm.requests if r.operation == "GetListItems"]
    assert [r.last_id for r in item_calls] == [0, 2]


def test_loop_terminates_immediately_on_an_empty_list(tmp_path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, KUNDEN)
    assert crawler.crawl_list(schema.list_info, schema) == 0
    assert crawler.state.get(KUNDEN).status == "complete"


def test_loop_resumes_from_a_non_zero_last_id(tmp_path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, CASES)
    crawler.state.update(CASES, status="in_progress", last_item_id=2, items_written=2)

    written = crawler.crawl_list(schema.list_info, schema, resume=True)

    assert written == 3  # 2 already accounted for, 1 fetched
    item_calls = [r for r in farm.requests if r.operation == "GetListItems"]
    assert [r.last_id for r in item_calls] == [2]


def test_completed_lists_are_skipped_on_resume(tmp_path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, CASES)
    crawler.state.update(CASES, status="complete", last_item_id=3, items_written=3)

    assert crawler.crawl_list(schema.list_info, schema, resume=True) == 0
    assert not [r for r in farm.requests if r.operation == "GetListItems"]
    assert crawler.report.lists_skipped == 1


def test_repeated_rows_abort_instead_of_looping_forever(
    tmp_path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, CASES)
    # A server that ignores the Gt filter would otherwise spin until the disk fills.
    farm.item_responses[(CASES, 2)] = "lists_getlistitems_page1.xml"

    with pytest.raises(RuntimeError, match="paging stalled"):
        crawler.crawl_list(schema.list_info, schema)

    assert len([r for r in farm.requests if r.operation == "GetListItems"]) == 2


def test_state_is_checkpointed_after_every_page(tmp_path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, page_size=2, download_files=False)
    schema = _schema(crawler, CASES)
    crawler.crawl_list(schema.list_info, schema)

    entry = crawler.state.get(CASES)
    assert entry.last_item_id == 3
    assert entry.items_written == 3
    assert entry.status == "complete"
    assert entry.last_full_crawl is not None
    # And it is on disk, not just in memory.
    assert crawler.state.path.exists()
    assert '"last_item_id": 3' in crawler.state.path.read_text(encoding="utf-8")
