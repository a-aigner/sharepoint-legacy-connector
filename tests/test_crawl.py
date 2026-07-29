"""Full orchestration across two webs × three lists, plus resume and sync."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CASES, CASES2, DOKUMENTE, KUNDEN, WEB1, WEB2, FakeFarm, make_settings
from spconnect.crawl import CrawlAborted, Crawler, display_url_for
from spconnect.models import ListInfo, web_id_for
from spconnect.services.lists import ListsService
from spconnect.transport import AuthenticationError, ServerVersion, Transport


def _crawler(tmp_path: Path, transport: Transport, **overrides) -> Crawler:
    return Crawler(make_settings(tmp_path, page_size=2, **overrides), transport)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def crawled(tmp_path: Path, farm: FakeFarm, transport: Transport) -> Crawler:
    crawler = _crawler(tmp_path, transport)
    crawler.crawl()
    crawler.write_manifest("crawl")
    return crawler


# --------------------------------------------------------------------------- #
# landing zone layout, §9
# --------------------------------------------------------------------------- #


def test_layout_is_exactly_as_specified(crawled: Crawler) -> None:
    root = crawled.landing.root
    for name in ("_manifest.json", "_state.json", "_graph.json", "_graph.mmd", "webs.json"):
        assert (root / name).exists(), name

    for web_url, guids in ((WEB1, (CASES, KUNDEN, DOKUMENTE)), (WEB2, (CASES2,))):
        assert (crawled.landing.web_dir(web_url) / "web.json").exists()
        for guid in guids:
            list_dir = crawled.landing.list_dir(web_url, guid)
            assert (list_dir / "list.json").exists()
            assert (list_dir / "items.jsonl").exists()
            assert (list_dir / "items_raw.jsonl").exists()


def test_both_webs_and_all_six_lists_are_crawled(crawled: Crawler) -> None:
    assert crawled.report.webs_discovered == 2
    assert crawled.report.lists_in_scope == 6
    assert crawled.report.lists_succeeded == 6
    assert crawled.report.lists_failed == 0


def test_every_item_appears_in_both_decoded_and_raw_form(crawled: Crawler) -> None:
    list_dir = crawled.landing.list_dir(WEB1, CASES)
    decoded = _lines(list_dir / "items.jsonl")
    raw = _lines(list_dir / "items_raw.jsonl")

    assert [d["item_id"] for d in decoded] == [1, 2, 3]
    assert [r["ID"] for r in raw] == ["1", "2", "3"]
    assert "MetaInfo" not in raw[0]
    assert raw[0]["Kunde"] == "42;#Müller Maschinenbau GmbH"


def test_decoded_item_shape(crawled: Crawler) -> None:
    item = _lines(crawled.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]

    assert item["doc_id"] == f"{web_id_for(WEB1)}:{CASES}:1"
    assert item["web_url"] == WEB1
    assert item["list_guid"] == CASES
    assert item["list_title"] == "Servicefälle"
    assert item["item_id"] == 1
    assert item["display_url"] == "http://sp/sites/service/Lists/Cases/DispForm.aspx?ID=1"
    assert item["content_type"] == "Element"
    assert item["created"] == "2009-03-14T08:11:00Z"
    assert item["modified"] == "2011-07-02T15:43:00Z"
    assert item["is_folder"] is False
    assert item["file_ref"] == "sites/service/Lists/Cases/1_.000"
    assert item["file_name"] == "1_.000"

    fields = item["fields"]
    assert fields["Title"] == "Getriebeschaden"
    assert fields["Case_x0020_Number"] == "SF-2009-0001"
    assert fields["Kunde"] == {"id": 42, "value": "Müller Maschinenbau GmbH"}
    assert fields["Betroffene_x0020_Anlagen"] == [
        {"id": 12, "value": "Presse Süd"},
        {"id": 13, "value": "Fräse Nord"},
        {"id": 14, "value": "Ofen Weiß"},
    ]
    assert fields["Kategorie"] == ["Reparatur", "Garantie"]
    assert fields["Kosten"] == 1234.5
    assert fields["Erledigt"] is True
    assert fields["Eingegangen"] == "2009-03-14T08:11:00Z"
    assert fields["Bearbeiter"] == {"id": 12, "value": "CONTOSO\\jdoe"}
    assert fields["Techniker"] == [
        {"id": 12, "value": "CONTOSO\\jdoe"},
        {"id": 15, "value": "CONTOSO\\mmueller"},
    ]
    assert fields["Kostensch_x00e4_tzung"] == 1469.055
    assert fields["Wiedervorlage"] == "2009-03-21T00:00:00Z"
    assert fields["Referenz"] == {
        "url": "http://intranet/sop/17",
        "description": "Reparaturanleitung Süd",
    }
    assert "<b>lautes</b>" in fields["Beschreibung"]
    assert "&amp;" in fields["Beschreibung"]
    assert "MetaInfo" not in fields

    assert item["field_display_names"]["Case_x0020_Number"] == "Case Number"
    assert item["field_display_names"]["Eingegangen"] == "Eingegangen am"


def test_doc_id_is_stable_across_two_consecutive_full_crawls(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    first = _crawler(tmp_path, transport)
    first.crawl()
    before = [i["doc_id"] for i in _lines(first.landing.list_dir(WEB1, CASES) / "items.jsonl")]

    second = _crawler(tmp_path, transport)
    second.crawl()
    after = [i["doc_id"] for i in _lines(second.landing.list_dir(WEB1, CASES) / "items.jsonl")]

    assert before == after
    assert len(after) == 3  # re-crawl replaces, never appends duplicates


def test_empty_list_produces_an_empty_but_present_file(crawled: Crawler) -> None:
    list_dir = crawled.landing.list_dir(WEB1, KUNDEN)
    assert (list_dir / "items.jsonl").read_text(encoding="utf-8") == ""
    assert json.loads((list_dir / "list.json").read_text(encoding="utf-8"))["list_info"]["title"] == "Kunden"


def test_webs_json_inventory(crawled: Crawler) -> None:
    payload = json.loads(crawled.landing.webs_path.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert [w["url"] for w in payload["webs"]] == [WEB1, WEB2]
    assert payload["webs"][0]["list_count"] == 3


def test_graph_is_emitted_in_both_formats(crawled: Crawler) -> None:
    graph = json.loads(crawled.landing.graph_json_path.read_text(encoding="utf-8"))
    assert len(graph["nodes"]) == 6
    assert any(e["dangling"] for e in graph["edges"])
    assert crawled.landing.graph_mmd_path.read_text(encoding="utf-8").startswith("graph LR")
    assert crawled.report.dangling_edges >= 1


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #


def test_attachment_urls_from_the_item_avoid_a_round_trip(crawled: Crawler, farm: FakeFarm) -> None:
    item = _lines(crawled.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    attachment = item["attachments"][0]
    assert attachment["filename"] == "foto.jpg"
    assert attachment["local_path"] == "files/1/foto.jpg"
    assert attachment["downloaded"] is True
    assert attachment["bytes"] == len(b"\xff\xd8\xff\xe0JPEG-ish bytes")
    assert len(attachment["sha256"]) == 64
    assert (crawled.landing.list_dir(WEB1, CASES) / "files" / "1" / "foto.jpg").exists()


def test_items_without_attachments_have_none(crawled: Crawler) -> None:
    assert _lines(crawled.landing.list_dir(WEB1, CASES) / "items.jsonl")[1]["attachments"] == []


def test_attachment_collection_is_the_fallback_only(crawled: Crawler, farm: FakeFarm) -> None:
    # Item 3 says it has attachments but carries no ows_AttachmentUrls.
    item = _lines(crawled.landing.list_dir(WEB1, CASES) / "items.jsonl")[2]
    assert [a["filename"] for a in item["attachments"]] == ["Prüfprotokoll.pdf", "messwerte.csv"]
    # Exactly one fallback call per web, for that one item — not one per item.
    assert farm.count("GetAttachmentCollection") == 2


def test_document_library_files_download_from_encoded_abs_url(crawled: Crawler) -> None:
    items = _lines(crawled.landing.list_dir(WEB1, DOKUMENTE) / "items.jsonl")
    doc, folder = items[0], items[1]

    assert doc["file_name"] == "Handbuch Straße.pdf"
    assert doc["attachments"][0]["filename"] == "Handbuch Straße.pdf"
    assert doc["attachments"][0]["downloaded"] is True
    assert doc["is_folder"] is False

    assert folder["is_folder"] is True
    assert folder["attachments"] == []  # folders have no bytes


def test_download_can_be_disabled(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()
    item = _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    assert item["attachments"][0]["skip_reason"] == "downloads_disabled"
    assert item["attachments"][0]["downloaded"] is False


def test_skipped_extensions_are_recorded_with_a_reason(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, skip_extensions=".jpg")
    crawler.crawl()
    item = _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    assert item["attachments"][0]["skip_reason"] == "extension_excluded:.jpg"
    assert crawler.report.skip_reasons["extension_excluded"] >= 1


def test_oversized_files_are_skipped_with_a_reason(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, max_file_mb=0.000001)
    crawler.crawl()
    item = _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    assert item["attachments"][0]["skip_reason"].startswith("too_large")
    assert not (crawled_files := crawler.landing.list_dir(WEB1, CASES) / "files" / "1").exists() or not list(
        crawled_files.iterdir()
    )


# --------------------------------------------------------------------------- #
# resume
# --------------------------------------------------------------------------- #


def test_resume_after_a_mid_list_crash_has_no_duplicates_and_no_gaps(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    _webs, by_web = crawler.discover()
    info = next(li for li in by_web[WEB1] if li.guid == CASES)
    schema = crawler.lists_service(WEB1).get_list_schema(info)

    # Die after the first page.
    farm.item_responses[(CASES, 2)] = "does-not-exist.xml"
    with pytest.raises(FileNotFoundError):
        crawler.crawl_list(schema.list_info, schema)

    list_dir = crawler.landing.list_dir(WEB1, CASES)
    assert [i["item_id"] for i in _lines(list_dir / "items.jsonl")] == [1, 2]
    assert crawler.state.get(CASES).last_item_id == 2

    # Restart in a fresh process: state comes off disk, not out of memory.
    farm.item_responses.pop((CASES, 2))
    resumed = _crawler(tmp_path, transport, download_files=False)
    resumed_schema = resumed.landing.read_list_schema(WEB1, CASES) or schema
    calls_before = farm.count("GetListItems")
    resumed.crawl_list(resumed_schema.list_info, resumed_schema, resume=True)

    ids = [i["item_id"] for i in _lines(list_dir / "items.jsonl")]
    assert ids == [1, 2, 3]  # no duplicates, no gaps
    assert [r["ID"] for r in _lines(list_dir / "items_raw.jsonl")] == ["1", "2", "3"]
    # Only the unfetched page was requested.
    assert farm.count("GetListItems") - calls_before == 1


def test_resume_truncates_a_half_written_trailing_line(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    _webs, by_web = crawler.discover()
    info = next(li for li in by_web[WEB1] if li.guid == CASES)
    schema = crawler.lists_service(WEB1).get_list_schema(info)
    crawler.crawl_list(schema.list_info, schema)

    list_dir = crawler.landing.list_dir(WEB1, CASES)
    with (list_dir / "items.jsonl").open("a", encoding="utf-8") as handle:
        handle.write('{"doc_id": "half writ')
    crawler.state.update(CASES, status="in_progress")

    crawler.crawl_list(schema.list_info, schema, resume=True)

    assert [i["item_id"] for i in _lines(list_dir / "items.jsonl")] == [1, 2, 3]


def test_full_crawl_without_resume_starts_the_list_clean(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    first = _crawler(tmp_path, transport, download_files=False)
    first.crawl()
    second = _crawler(tmp_path, transport, download_files=False)
    second.crawl()
    ids = [i["item_id"] for i in _lines(second.landing.list_dir(WEB1, CASES) / "items.jsonl")]
    assert ids == [1, 2, 3]


# --------------------------------------------------------------------------- #
# error policy
# --------------------------------------------------------------------------- #


def test_one_failing_list_does_not_abort_the_crawl(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    farm.fail_on["GetListItems"] = "soap_fault.xml"

    report = crawler.crawl()

    assert report.lists_failed == 1
    assert report.lists_succeeded == 5
    assert report.errors[0].error_type == "SharePointSoapFault"
    assert report.errors[0].operation == "GetListItems"
    assert crawler.state.get(CASES).status == "failed"

    manifest = crawler.write_manifest("crawl")
    assert len(manifest.errors) == 1
    assert manifest.counts["lists_failed"] == 1


def test_auth_failure_aborts_immediately(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    farm.fail_on["GetListItems"] = AuthenticationError("HTTP 401")

    with pytest.raises(CrawlAborted):
        crawler.crawl()

    # It stopped at the first list rather than marking 87 of them failed.
    assert crawler.report.lists_failed == 0


def test_manifest_records_the_run(crawled: Crawler) -> None:
    manifest = json.loads(crawled.landing.manifest_path.read_text(encoding="utf-8"))
    assert manifest["command"] == "crawl"
    assert manifest["base_url"] == WEB1
    assert manifest["server_version"]["major"] == 12
    assert manifest["server_version"]["product"] == "WSS 3.0 / MOSS 2007"
    # (3 cases + 0 kunden + 2 dokumente) per web, twice
    assert manifest["counts"]["items_written"] == 10
    assert manifest["config"]["password"] == "***REDACTED***"
    assert manifest["lists_with_unique_scopes"] == [f"{WEB1} :: Servicefälle"]
    assert manifest["finished_at"] is not None


# --------------------------------------------------------------------------- #
# incremental sync
# --------------------------------------------------------------------------- #


def test_sync_applies_updates_and_records_deletes(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()
    # A completed crawl primes a token so the next pass can be incremental.
    assert crawler.state.get(CASES).change_token is None

    syncer = _crawler(tmp_path, transport, download_files=False)
    syncer.state.update(CASES, change_token="1;3;stale-but-valid")
    syncer.sync()

    items = _lines(syncer.landing.list_dir(WEB1, CASES) / "items.jsonl")
    by_id = {i["item_id"]: i for i in items}

    assert 2 not in by_id  # deleted item is gone from the landing zone
    assert by_id[1]["fields"]["Title"] == "Getriebeschaden (korrigiert)"
    assert by_id[1]["fields"]["Kosten"] == 1500.0
    assert 3 in by_id  # untouched item survives
    assert syncer.report.items_deleted == 1
    assert syncer.state.get(CASES).change_token.startswith("1;3;11111111")


def test_sync_removes_deleted_items_from_the_raw_file_too(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()
    syncer = _crawler(tmp_path, transport, download_files=False)
    syncer.state.update(CASES, change_token="1;3;stale-but-valid")
    syncer.sync()

    raw_ids = [r["ID"] for r in _lines(syncer.landing.list_dir(WEB1, CASES) / "items_raw.jsonl")]
    assert "2" not in raw_ids


def test_sync_without_a_token_falls_back_to_a_full_crawl(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.sync()
    ids = [i["item_id"] for i in _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")]
    assert ids == [1, 2, 3]


def test_sync_falls_back_when_the_token_is_invalid(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()

    syncer = _crawler(tmp_path, transport, download_files=False)
    syncer.state.update(CASES, change_token="expired")
    farm.changes_fixture = "lists_getlistitemchangessincetoken_invalid.xml"
    syncer.sync()

    assert any("change token invalid" in w for w in syncer.report.warnings)
    ids = [i["item_id"] for i in _lines(syncer.landing.list_dir(WEB1, CASES) / "items.jsonl")]
    assert ids == [1, 2, 3]


def test_sync_falls_back_on_a_pre_wss3_server(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()

    syncer = _crawler(tmp_path, transport, download_files=False)
    syncer.server_version = ServerVersion(raw="6.0.2.6568", major=6)
    syncer.state.update(CASES, change_token="whatever", status="complete")
    syncer.sync()

    assert farm.count("GetListItemChangesSinceToken") == 0
    assert any("predates change tokens" in w for w in syncer.report.warnings)


def test_sync_rejected_token_soap_fault_falls_back(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.crawl()

    syncer = _crawler(tmp_path, transport, download_files=False)
    syncer.state.update(CASES, change_token="expired")
    farm.fail_on["GetListItemChangesSinceToken"] = "soap_fault.xml"
    syncer.sync()

    assert any("rejected" in w for w in syncer.report.warnings)


# --------------------------------------------------------------------------- #
# dry run and verify-time
# --------------------------------------------------------------------------- #


def test_dry_run_fetches_no_items(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport)
    plan = crawler.dry_run()

    assert farm.count("GetListItems") == 0
    assert plan["webs"] == 2
    assert plan["lists"] == 6
    assert plan["items"] == 3 + 0 + 2 + 3 + 0 + 2
    assert plan["estimated_requests"] > 0
    cases_row = next(r for r in plan["rows"] if r["list_guid"] == CASES)
    assert cases_row["pages"] == 2  # 3 items at page size 2
    assert cases_row["has_unique_scopes"] is True


def test_verify_time_reports_raw_and_decoded(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport)
    result = crawler.verify_time("Servicefälle", 1)

    assert result["item_id"] == 1
    assert result["display_url"].endswith("DispForm.aspx?ID=1")
    assert result["query_options"] == "DateInUtc=TRUE"

    eingegangen = next(f for f in result["fields"] if f["field"] == "Eingegangen")
    assert eingegangen["raw_wire_value"] == "2009-03-14T08:11:00Z"
    assert eingegangen["decoded_utc"] == "2009-03-14T08:11:00Z"
    assert eingegangen["display_name"] == "Eingegangen am"


def test_verify_time_rejects_an_unknown_list(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    with pytest.raises(ValueError, match="no in-scope list"):
        _crawler(tmp_path, transport).verify_time("Gibt Es Nicht", 1)


# --------------------------------------------------------------------------- #
# display urls
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (
            ListInfo(guid=CASES, default_view_url="/sites/service/Lists/Cases/AllItems.aspx"),
            "http://sp/sites/service/Lists/Cases/DispForm.aspx?ID=7",
        ),
        (
            ListInfo(guid=CASES, base_type="1", root_folder="/sites/service/Dokumente"),
            "http://sp/sites/service/Dokumente/Forms/DispForm.aspx?ID=7",
        ),
        (
            ListInfo(guid=CASES, default_view_url="/sites/service/Lists/Fälle Süd/AllItems.aspx"),
            "http://sp/sites/service/Lists/F%C3%A4lle%20S%C3%BCd/DispForm.aspx?ID=7",
        ),
        (ListInfo(guid=CASES), "http://sp/sites/service/DispForm.aspx?ID=7"),
    ],
)
def test_display_url_for(info: ListInfo, expected: str) -> None:
    assert display_url_for(info, WEB1, 7) == expected


# --------------------------------------------------------------------------- #
# web discovery on a pre-WSS3 build
# --------------------------------------------------------------------------- #


def test_crawl_works_on_a_wss2_server_via_the_get_web_collection_walk(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.server_version = ServerVersion(raw="6.0.2.6568", major=6)

    report = crawler.crawl()

    assert report.web_discovery_method == "GetWebCollection"
    assert report.webs_discovered == 2
    assert report.lists_succeeded == 6
    assert farm.count("GetAllSubWebCollection") == 0
    assert any("predates GetAllSubWebCollection" in w for w in report.warnings)
    # And the landing zone is identical to the WSS 3.0 path.
    assert [i["item_id"] for i in _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")] == [1, 2, 3]


def test_discovery_falls_back_when_the_operation_is_missing(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    # Version header says WSS 3.0, but the operation is not actually there.
    farm.always_fail["GetAllSubWebCollection"] = "soap_fault_unknown_action.xml"
    crawler = _crawler(tmp_path, transport, download_files=False)

    webs, lists_by_web = crawler.discover()

    assert [w.url for w in webs] == [WEB1, WEB2]
    assert crawler.report.web_discovery_method == "GetWebCollection"
    assert sum(len(v) for v in lists_by_web.values()) == 6
    assert any("falling back" in w for w in crawler.report.warnings)


def test_manifest_records_how_webs_were_discovered(crawled: Crawler) -> None:
    manifest = json.loads(crawled.landing.manifest_path.read_text(encoding="utf-8"))
    assert manifest["web_discovery_method"] == "GetAllSubWebCollection"
    assert manifest["server_version"]["supports_all_sub_web_collection"] is True


# --------------------------------------------------------------------------- #
# SharePoint 2010 specifics
# --------------------------------------------------------------------------- #


def _as_2010(crawler: Crawler) -> Crawler:
    crawler.server_version = ServerVersion(raw="14.0.4762.1000", major=14)
    return crawler


def test_oversized_lists_are_flagged_at_discovery_not_after_an_hour_of_crawling(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _as_2010(_crawler(tmp_path, transport, download_files=False))
    original = ListsService.get_list_collection

    def inflated(self):
        lists = original(self)
        for info in lists:
            if info.guid == CASES:
                info.item_count = 45_231
        return lists

    ListsService.get_list_collection = inflated
    try:
        crawler.discover()
    finally:
        ListsService.get_list_collection = original

    assert any("45,231 items" in entry for entry in crawler.report.large_lists)
    assert any("list view threshold" in w for w in crawler.report.warnings)
    assert farm.count("GetListItems") == 0  # flagged before a single item was fetched


def test_no_threshold_warning_on_a_wss3_farm(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, download_files=False)
    crawler.server_version = ServerVersion(raw="12.0.0.6421", major=12)
    original = ListsService.get_list_collection

    def inflated(self):
        lists = original(self)
        for info in lists:
            info.item_count = 45_231
        return lists

    ListsService.get_list_collection = inflated
    try:
        crawler.discover()
    finally:
        ListsService.get_list_collection = original

    assert crawler.report.large_lists == []  # WSS 3.0 has no such limit


def test_a_throttled_list_is_named_as_such_and_does_not_abort_the_crawl(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _as_2010(_crawler(tmp_path, transport, download_files=False))
    farm.fail_on["GetListItems"] = "soap_fault_threshold.xml"

    report = crawler.crawl()

    assert report.throttled_lists == [f"{WEB1} :: Servicefälle"]
    assert report.lists_failed == 1
    assert report.lists_succeeded == 5  # the rest of the farm still lands


def test_a_normal_fault_is_not_reported_as_throttling(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _as_2010(_crawler(tmp_path, transport, download_files=False))
    farm.fail_on["GetListItems"] = "soap_fault.xml"
    report = crawler.crawl()
    assert report.throttled_lists == []
    assert report.lists_failed == 1


def test_2010_uses_the_single_call_discovery_and_real_incremental_sync(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _as_2010(_crawler(tmp_path, transport, download_files=False))
    crawler.crawl()

    assert crawler.report.web_discovery_method == "GetAllSubWebCollection"

    syncer = _as_2010(_crawler(tmp_path, transport, download_files=False))
    syncer.state.update(CASES, change_token="1;3;primed")
    syncer.sync()

    # Real incremental sync, not the pre-WSS3 full-crawl fallback.
    assert farm.count("GetListItemChangesSinceToken") > 0
    assert syncer.report.items_deleted == 1
