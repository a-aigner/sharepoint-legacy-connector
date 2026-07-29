"""The ``ListData.svc`` REST backend, and its equivalence with the SOAP one.

The point of having two backends is being able to compare them, so the sharpest
tests here are the ones asserting that both produce the *same landing zone*.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import CASES, WEB1, FakeFarm, make_settings
from spconnect.crawl import Crawler
from spconnect.models import ListSchema
from spconnect.services.odata import (
    ODataRowMapper,
    ODataService,
    ODataUnavailable,
    normalise_name,
    parse_odata_datetime,
)
from spconnect.transport import Transport


@pytest.fixture
def service(farm: FakeFarm, transport: Transport) -> ODataService:
    return ODataService(transport, WEB1)


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _crawler(tmp_path: Path, transport: Transport, **overrides) -> Crawler:
    return Crawler(make_settings(tmp_path, page_size=2, download_files=False, **overrides), transport)


# --------------------------------------------------------------------------- #
# dates
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/Date(1237018260000)/", datetime(2009, 3, 14, 8, 11, tzinfo=UTC)),
        ("/Date(0)/", datetime(1970, 1, 1, tzinfo=UTC)),
        ("2009-03-14T08:11:00Z", datetime(2009, 3, 14, 8, 11, tzinfo=UTC)),
        ("2009-03-14T08:11:00", datetime(2009, 3, 14, 8, 11, tzinfo=UTC)),
    ],
)
def test_parse_odata_datetime(raw: str, expected: datetime) -> None:
    assert parse_odata_datetime(raw) == expected


def test_odata_datetime_with_offset_is_normalised_to_utc() -> None:
    # /Date(ms+0060)/ means the ms are local; the offset takes it back to UTC.
    assert parse_odata_datetime("/Date(1237018260000+0060)/") == datetime(2009, 3, 14, 7, 11, tzinfo=UTC)


@pytest.mark.parametrize("raw", ["", "not a date", "/Date(abc)/"])
def test_unparseable_dates_return_none(raw: str) -> None:
    assert parse_odata_datetime(raw) is None


# --------------------------------------------------------------------------- #
# entity set naming — the documented trap for a German farm
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Servicefälle", "Servicefälle"),
        ("Case Number", "CaseNumber"),
        ("Fälle 2008", "Flle2008"),  # umlaut dropped by the sanitiser
        ("Prüfberichte", "PrÜfberichte"),  # umlaut capitalised by the sanitiser
    ],
)
def test_normalise_name_matches_across_the_sanitiser(a: str, b: str) -> None:
    # One of the two folds must match, which is why entity_set_for tries both.
    assert normalise_name(a, ascii_only=True) == normalise_name(b, ascii_only=True) or normalise_name(
        a
    ) == normalise_name(b)


def test_entity_sets_are_read_not_guessed(service: ODataService, farm: FakeFarm) -> None:
    assert service.entity_sets() == ["Servicefälle", "Kunden", "Dokumente", "MasterPageGallery"]
    # Cached: the service document is fetched once per web.
    service.entity_sets()
    assert len([u for u in farm.odata_requests if u.rstrip("/").endswith("ListData.svc")]) == 1


def test_entity_set_lookup_survives_umlauts(service: ODataService) -> None:
    assert service.entity_set_for("Servicefälle") == "Servicefälle"
    assert service.entity_set_for("Kunden") == "Kunden"


def test_entity_set_lookup_reports_a_miss_rather_than_guessing(service: ODataService) -> None:
    assert service.entity_set_for("Gibt Es Nicht") is None


def test_availability_probe_never_raises(service: ODataService, farm: FakeFarm) -> None:
    ok, detail = service.available()
    assert ok is True
    assert "4 entity sets" in detail

    farm.odata_broken = "odata_not_available.html"
    broken = ODataService(service.transport, WEB1)
    ok, detail = broken.available()
    assert ok is False
    assert detail


def test_a_404_means_the_feature_is_off_not_that_the_crawl_failed(
    service: ODataService, farm: FakeFarm
) -> None:
    farm.odata_broken = "odata_not_available.html"
    with pytest.raises(ODataUnavailable, match="OData feature is disabled"):
        ODataService(service.transport, WEB1).entity_sets()


def test_html_body_with_a_200_is_also_typed(service: ODataService, farm: FakeFarm) -> None:
    # Some proxies answer 200 with an error page rather than a status code.
    farm.odata_broken = "odata_not_available.html"
    farm.odata_broken_status = 200
    with pytest.raises(ODataUnavailable, match="did not return JSON"):
        ODataService(service.transport, WEB1).entity_sets()


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #


def test_pages_on_id_like_the_soap_backend(service: ODataService, farm: FakeFarm) -> None:
    page = service.get_items("Servicefälle", last_id=0, top=2)
    assert [r["Id"] for r in page.rows] == [1, 2]
    assert page.max_id == 2
    assert "Id%20gt%200" in farm.odata_requests[-1]
    assert "$orderby=Id" in farm.odata_requests[-1]
    assert "$top=2" in farm.odata_requests[-1]


def test_second_page_resumes_from_the_checkpoint(service: ODataService) -> None:
    page = service.get_items("Servicefälle", last_id=2, top=2)
    assert [r["Id"] for r in page.rows] == [3]
    assert page.max_id == 3


def test_empty_result_terminates(service: ODataService) -> None:
    page = service.get_items("Kunden", last_id=0, top=2)
    assert page.rows == []
    assert page.max_id is None


def test_expand_is_requested_for_lookups(service: ODataService, farm: FakeFarm) -> None:
    service.get_items("Servicefälle", last_id=0, top=2, expand=["Kunde"])
    assert "$expand=Kunde" in farm.odata_requests[-1]


# --------------------------------------------------------------------------- #
# mapping OData properties back to internal names
# --------------------------------------------------------------------------- #


@pytest.fixture
def mapper(farm: FakeFarm, transport: Transport) -> ODataRowMapper:
    from spconnect.services.lists import ListsService

    service = ListsService(transport, WEB1)
    info = next(li for li in service.get_list_collection() if li.guid == CASES)
    return ODataRowMapper(service.get_list_schema(info))


def test_sanitised_property_names_map_back_to_internal_names(mapper: ODataRowMapper) -> None:
    # "CaseNumber" (OData) must land on "Case_x0020_Number" (the contract key).
    assert mapper.internal_name("CaseNumber") == "Case_x0020_Number"
    assert mapper.internal_name("Title") == "Title"
    assert mapper.internal_name("Kunde") == "Kunde"


def test_map_row_produces_contract_shaped_keys(mapper: ODataRowMapper) -> None:
    entity = json.loads((Path(__file__).parent / "fixtures" / "odata_cases_page1.json").read_text())
    raw, decoded = mapper.map_row(entity["d"]["results"][0])

    assert "__metadata" not in raw  # control keys never reach disk
    assert decoded["Title"] == "Getriebeschaden"
    assert decoded["Case_x0020_Number"] == "SF-2009-0001"
    assert decoded["Kategorie"] == ["Reparatur", "Garantie"]
    assert decoded["Erledigt"] is True
    assert decoded["Eingegangen"] == datetime(2009, 3, 14, 8, 11, tzinfo=UTC)


def test_lookups_keep_the_durable_id(mapper: ODataRowMapper) -> None:
    entity = json.loads((Path(__file__).parent / "fixtures" / "odata_cases_page1.json").read_text())
    _raw, decoded = mapper.map_row(entity["d"]["results"][0])
    # Same shape as the SOAP decoder: id is durable, label is a snapshot.
    assert decoded["Kunde"] == {"id": 42, "value": "Müller Maschinenbau GmbH"}


def test_unexpanded_navigation_properties_are_dropped_not_serialised(mapper: ODataRowMapper) -> None:
    entity = json.loads((Path(__file__).parent / "fixtures" / "odata_cases_page1.json").read_text())
    _raw, decoded = mapper.map_row(entity["d"]["results"][0])
    assert not any(isinstance(v, dict) and "__deferred" in v for v in decoded.values())


def test_unmappable_properties_are_kept_under_their_odata_name(mapper: ODataRowMapper) -> None:
    _raw, decoded = mapper.map_row({"Id": 1, "SomeFutureColumn": "value"})
    # Losing a column silently is the one unacceptable outcome.
    assert decoded["SomeFutureColumn"] == "value"
    assert "SomeFutureColumn" in mapper.unmapped


def test_mapper_without_a_schema_still_produces_rows() -> None:
    mapper = ODataRowMapper(
        ListSchema(list_info=__import__("spconnect.models", fromlist=["ListInfo"]).ListInfo(guid=CASES))
    )
    _raw, decoded = mapper.map_row({"Id": 7, "Title": "x"})
    assert decoded["ID"] == 7


# --------------------------------------------------------------------------- #
# equivalence: the whole point of shipping both
# --------------------------------------------------------------------------- #


def test_odata_backend_produces_the_documented_landing_zone(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, api_mode="odata")
    crawler.crawl()

    list_dir = crawler.landing.list_dir(WEB1, CASES)
    items = _lines(list_dir / "items.jsonl")

    assert [i["item_id"] for i in items] == [1, 2, 3]
    assert items[0]["list_guid"] == CASES
    assert items[0]["list_title"] == "Servicefälle"
    assert items[0]["display_url"].endswith("DispForm.aspx?ID=1")
    assert items[0]["fields"]["Title"] == "Getriebeschaden"
    assert (list_dir / "items_raw.jsonl").exists()
    assert (list_dir / "list.json").exists()


def test_doc_ids_are_identical_across_both_backends(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    # The downstream pipeline upserts on doc_id, so the two backends MUST agree
    # or switching would duplicate every document in the vector DB.
    soap = _crawler(tmp_path / "soap", transport, api_mode="soap")
    soap.settings.state_file = tmp_path / "soap" / "landing" / "_state.json"
    soap.crawl()

    rest = _crawler(tmp_path / "rest", transport, api_mode="odata")
    rest.settings.state_file = tmp_path / "rest" / "landing" / "_state.json"
    rest.crawl()

    soap_ids = [i["doc_id"] for i in _lines(soap.landing.list_dir(WEB1, CASES) / "items.jsonl")]
    rest_ids = [i["doc_id"] for i in _lines(rest.landing.list_dir(WEB1, CASES) / "items.jsonl")]
    assert soap_ids == rest_ids


def test_both_backends_agree_on_the_values_that_matter(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    soap = _crawler(tmp_path / "s", transport, api_mode="soap")
    soap.settings.state_file = tmp_path / "s" / "landing" / "_state.json"
    soap.crawl()
    rest = _crawler(tmp_path / "r", transport, api_mode="odata")
    rest.settings.state_file = tmp_path / "r" / "landing" / "_state.json"
    rest.crawl()

    s_item = _lines(soap.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    r_item = _lines(rest.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]

    for key in ("Title", "Case_x0020_Number", "Kategorie", "Erledigt", "Kunde"):
        assert s_item["fields"][key] == r_item["fields"][key], key
    assert s_item["fields"]["Eingegangen"] == r_item["fields"]["Eingegangen"]
    assert s_item["created"] == r_item["created"]


def test_a_list_without_an_entity_set_falls_back_to_soap(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, api_mode="odata")
    crawler.crawl()

    # "Dokumente" has an entity set; the web2 lists do not appear in the
    # fixture service document, so they must still land via SOAP.
    assert crawler.report.odata_fallbacks
    assert any("used SOAP for this list" in w for w in crawler.report.warnings)
    assert crawler.report.lists_succeeded == 6  # nothing was skipped


def test_resume_works_under_the_rest_backend(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport, api_mode="odata")
    crawler.crawl()
    ids_before = [i["item_id"] for i in _lines(crawler.landing.list_dir(WEB1, CASES) / "items.jsonl")]

    again = _crawler(tmp_path, transport, api_mode="odata")
    again.crawl(resume=True)
    ids_after = [i["item_id"] for i in _lines(again.landing.list_dir(WEB1, CASES) / "items.jsonl")]

    assert ids_before == ids_after == [1, 2, 3]


def test_manifest_records_which_backend_produced_the_data(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    crawler = _crawler(tmp_path, transport, api_mode="odata")
    crawler.crawl()
    manifest = crawler.write_manifest("crawl")
    assert manifest.api_mode == "odata"
    assert json.loads(crawler.landing.manifest_path.read_text(encoding="utf-8"))["api_mode"] == "odata"


def test_soap_remains_the_default(tmp_path: Path, farm: FakeFarm, transport: Transport) -> None:
    crawler = _crawler(tmp_path, transport)
    assert crawler.settings.api_mode == "soap"
    crawler.crawl()
    assert farm.odata_requests == []  # REST is opt-in, never touched by default


def test_decimals_are_coerced_to_numbers_like_the_soap_backend(mapper: ODataRowMapper) -> None:
    # OData v2 serialises Edm.Decimal as a *string* to protect precision. Left
    # alone, "1234.5000000000000" would silently replace 1234.5 the moment
    # anyone switched backends.
    _raw, decoded = mapper.map_row({"Id": 1, "Kosten": "1234.5000000000000"})
    assert decoded["Kosten"] == 1234.5
    assert isinstance(decoded["Kosten"], float)


def test_unparseable_numerics_are_kept_rather_than_lost(mapper: ODataRowMapper) -> None:
    _raw, decoded = mapper.map_row({"Id": 1, "Kosten": "n/a"})
    assert decoded["Kosten"] == "n/a"


def test_numeric_fields_agree_across_both_backends(
    tmp_path: Path, farm: FakeFarm, transport: Transport
) -> None:
    soap = _crawler(tmp_path / "s", transport, api_mode="soap")
    soap.settings.state_file = tmp_path / "s" / "landing" / "_state.json"
    soap.crawl()
    rest = _crawler(tmp_path / "r", transport, api_mode="odata")
    rest.settings.state_file = tmp_path / "r" / "landing" / "_state.json"
    rest.crawl()

    s_item = _lines(soap.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    r_item = _lines(rest.landing.list_dir(WEB1, CASES) / "items.jsonl")[0]
    assert s_item["fields"]["Kosten"] == r_item["fields"]["Kosten"] == 1234.5
