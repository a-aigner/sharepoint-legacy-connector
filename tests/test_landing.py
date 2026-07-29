"""The landing zone contract: layout, JSONL writing, truncation, stats."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import CASES, WEB1, WEB2
from spconnect.landing import LandingZone, web_slug, write_json_atomic
from spconnect.models import (
    AttachmentRecord,
    ItemRecord,
    ListInfo,
    ListSchema,
    Manifest,
    WebRef,
    guid_slug,
    web_id_for,
)
from spconnect.schema import build_lookup_graph
from spconnect.state import StateStore, utcnow


def _record(item_id: int, title: str = "Getriebeschaden") -> ItemRecord:
    return ItemRecord(
        doc_id=f"{web_id_for(WEB1)}:{CASES}:{item_id}",
        web_url=WEB1,
        web_id=web_id_for(WEB1),
        list_guid=CASES,
        list_title="Servicefälle",
        item_id=item_id,
        display_url=f"{WEB1}/Lists/Cases/DispForm.aspx?ID={item_id}",
        fields={"Title": title, "Kunde": {"id": 42, "value": "Müller GmbH"}},
        field_display_names={"Title": "Titel"},
    )


@pytest.fixture
def zone(tmp_path: Path) -> LandingZone:
    zone = LandingZone(tmp_path / "landing")
    zone.ensure()
    return zone


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (WEB1, "sp_sites_service"),
        (WEB2, "sp_sites_service_cases2008"),
        ("http://sp", "sp"),
        ("http://sp/sites/Fälle 2008/", "sp_sites_F_lle_2008"),
    ],
)
def test_web_slug_is_deterministic_and_filesystem_safe(url: str, expected: str) -> None:
    assert web_slug(url) == expected
    assert web_slug(url) == web_slug(url + "/")


def test_layout_matches_the_documented_contract(zone: LandingZone) -> None:
    list_dir = zone.list_dir(WEB1, CASES)
    assert list_dir == zone.root / "webs" / "sp_sites_service" / "lists" / guid_slug(CASES)
    assert zone.manifest_path.name == "_manifest.json"
    assert zone.graph_json_path.name == "_graph.json"
    assert zone.graph_mmd_path.name == "_graph.mmd"
    assert zone.webs_path.name == "webs.json"


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #


def test_writer_emits_both_decoded_and_raw_lines(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        writer.write(_record(1), {"ID": "1", "Title": "Getriebeschaden"})
        writer.write(_record(2, "Ölwechsel"), {"ID": "2", "Title": "Ölwechsel"})

    decoded = [json.loads(line) for line in writer.items_path.read_text(encoding="utf-8").splitlines()]
    raw = [json.loads(line) for line in writer.items_raw_path.read_text(encoding="utf-8").splitlines()]
    assert [d["item_id"] for d in decoded] == [1, 2]
    assert [r["ID"] for r in raw] == ["1", "2"]
    assert decoded[1]["fields"]["Title"] == "Ölwechsel"


def test_jsonl_is_written_and_flushed_per_line(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES).open()
    writer.write(_record(1), {"ID": "1"})
    # Readable before close: nothing is buffered in memory waiting for the end.
    assert writer.items_path.read_text(encoding="utf-8").count("\n") == 1
    writer.close()


def test_umlauts_survive_the_round_trip(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        writer.write(_record(1, "Ölwechsel überfällig — Straße"), {"ID": "1"})
    text = writer.items_path.read_text(encoding="utf-8")
    assert "Ölwechsel überfällig — Straße" in text  # not Ö-escaped
    assert json.loads(text)["fields"]["Title"] == "Ölwechsel überfällig — Straße"


def test_attachments_are_serialised_with_hash_and_size(zone: LandingZone) -> None:
    record = _record(1)
    record.attachments = [
        AttachmentRecord(
            filename="foto.jpg",
            url="http://sp/a/1/foto.jpg",
            local_path="files/1/foto.jpg",
            bytes=20481,
            sha256="deadbeef",
            downloaded=True,
        ),
        AttachmentRecord(
            filename="setup.exe", url="http://sp/a/1/setup.exe", skip_reason="extension_excluded:.exe"
        ),
    ]
    writer = zone.writer(WEB1, CASES)
    with writer:
        writer.write(record, {"ID": "1"})

    line = json.loads(writer.items_path.read_text(encoding="utf-8"))
    assert line["attachments"][0] == {
        "filename": "foto.jpg",
        "url": "http://sp/a/1/foto.jpg",
        "local_path": "files/1/foto.jpg",
        "bytes": 20481,
        "sha256": "deadbeef",
        "downloaded": True,
        "skip_reason": None,
    }
    assert line["attachments"][1]["skip_reason"] == "extension_excluded:.exe"


def test_writing_before_open_is_an_error(zone: LandingZone) -> None:
    with pytest.raises(RuntimeError):
        zone.writer(WEB1, CASES).write(_record(1), {"ID": "1"})


# --------------------------------------------------------------------------- #
# resume support
# --------------------------------------------------------------------------- #


def test_truncate_drops_rows_beyond_the_checkpoint(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        for i in (1, 2, 3, 4):
            writer.write(_record(i), {"ID": str(i)})

    removed = writer.truncate_after(2)

    assert removed == 4  # two lines from each file
    assert [json.loads(x)["item_id"] for x in writer.items_path.read_text().splitlines()] == [1, 2]
    assert [json.loads(x)["ID"] for x in writer.items_raw_path.read_text().splitlines()] == ["1", "2"]


def test_truncate_drops_a_half_written_trailing_line(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        writer.write(_record(1), {"ID": "1"})
    # Simulate a process killed mid-write.
    with writer.items_path.open("a", encoding="utf-8") as handle:
        handle.write('{"doc_id": "trunca')

    writer.truncate_after(1)

    lines = writer.items_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["item_id"] == 1


def test_truncate_on_a_fresh_list_is_a_no_op(zone: LandingZone) -> None:
    assert zone.writer(WEB1, CASES).truncate_after(10) == 0


def test_reset_starts_the_list_from_scratch(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        writer.write(_record(1), {"ID": "1"})
    writer.reset()
    assert not writer.items_path.exists()
    assert not writer.items_raw_path.exists()


def test_delete_items_removes_them_from_both_files(zone: LandingZone) -> None:
    writer = zone.writer(WEB1, CASES)
    with writer:
        for i in (1, 2, 3):
            writer.write(_record(i), {"ID": str(i)})

    removed = writer.delete_items({2})

    assert removed == 2
    assert [json.loads(x)["item_id"] for x in writer.items_path.read_text().splitlines()] == [1, 3]
    assert writer.existing_item_ids() == {1, 3}


def test_delete_items_with_an_empty_set_touches_nothing(zone: LandingZone) -> None:
    assert zone.writer(WEB1, CASES).delete_items(set()) == 0


# --------------------------------------------------------------------------- #
# json artifacts
# --------------------------------------------------------------------------- #


def test_write_webs_and_web(zone: LandingZone) -> None:
    web = WebRef(title="Service", url=WEB1)
    info = ListInfo(guid=CASES, title="Servicefälle", web_url=WEB1, item_count=3)
    zone.write_webs([web], {WEB1: [info]})
    zone.write_web(web, [info])

    payload = json.loads(zone.webs_path.read_text(encoding="utf-8"))
    assert payload["count"] == 1
    assert payload["webs"][0]["url"] == WEB1
    assert payload["webs"][0]["list_count"] == 1

    web_json = json.loads((zone.web_dir(WEB1) / "web.json").read_text(encoding="utf-8"))
    assert web_json["lists"][0]["title"] == "Servicefälle"
    assert web_json["web_id"] == web_id_for(WEB1)


def test_list_schema_round_trips(zone: LandingZone) -> None:
    schema = ListSchema(list_info=ListInfo(guid=CASES, title="Servicefälle", web_url=WEB1))
    path = zone.write_list_schema(schema)
    assert path.name == "list.json"

    loaded = zone.read_list_schema(WEB1, CASES)
    assert loaded is not None
    assert loaded.list_info.title == "Servicefälle"
    assert [s.list_info.guid for s in zone.iter_list_schemas()] == [CASES]


def test_unreadable_schema_is_reported_not_raised(zone: LandingZone) -> None:
    path = zone.list_dir(WEB1, CASES) / "list.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert zone.read_list_schema(WEB1, CASES) is None
    assert list(zone.iter_list_schemas()) == []


def test_graph_is_written_in_both_formats(zone: LandingZone) -> None:
    schema = ListSchema(list_info=ListInfo(guid=CASES, title="Servicefälle", web_url=WEB1))
    zone.write_graph(build_lookup_graph([schema]))
    assert json.loads(zone.graph_json_path.read_text(encoding="utf-8"))["nodes"][0]["title"] == "Servicefälle"
    assert zone.graph_mmd_path.read_text(encoding="utf-8").startswith("graph LR")


def test_manifest_round_trips_and_redacts(zone: LandingZone) -> None:
    manifest = Manifest(
        command="crawl",
        spconnect_version="0.1.0",
        started_at=utcnow(),
        counts={"items_written": 3},
        config={"password": "***REDACTED***"},
    )
    zone.write_manifest(manifest)
    loaded = zone.read_manifest()
    assert loaded is not None
    assert loaded.counts["items_written"] == 3


def test_read_manifest_of_a_corrupt_file_returns_none(zone: LandingZone) -> None:
    zone.manifest_path.write_text("nope", encoding="utf-8")
    assert zone.read_manifest() is None


def test_write_json_atomic_leaves_no_temp_files(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def test_state_is_written_atomically(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "landing" / "_state.json")
    store.update(CASES, list_title="Servicefälle", last_item_id=200, status="in_progress")
    store.save()

    assert store.path.exists()
    assert not [p for p in store.path.parent.iterdir() if p.name.startswith(".state-")]

    reloaded = StateStore(store.path)
    assert reloaded.get(CASES).last_item_id == 200
    assert reloaded.get(CASES).status == "in_progress"


def test_corrupt_state_is_set_aside_not_fatal(tmp_path: Path) -> None:
    path = tmp_path / "_state.json"
    path.write_text("{ broken", encoding="utf-8")
    store = StateStore(path)
    assert store.state.lists == {}
    assert path.with_suffix(".json.corrupt").exists()


def test_state_reset(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "_state.json")
    store.update(CASES, last_item_id=99, status="failed")
    assert store.reset(CASES).last_item_id == 0


# --------------------------------------------------------------------------- #
# stats
# --------------------------------------------------------------------------- #


def test_stats_counts_what_is_on_disk(zone: LandingZone) -> None:
    zone.write_list_schema(ListSchema(list_info=ListInfo(guid=CASES, title="Servicefälle", web_url=WEB1)))
    writer = zone.writer(WEB1, CASES)
    with writer:
        for i in (1, 2):
            writer.write(_record(i), {"ID": str(i)})
    files_dir = writer.item_files_dir(1)
    files_dir.mkdir(parents=True)
    (files_dir / "foto.jpg").write_bytes(b"0123456789")

    stats = zone.stats()
    assert stats["webs"] == 1
    assert stats["lists"] == 1
    assert stats["items"] == 2
    assert stats["files"] == 1
    assert stats["file_bytes"] == 10
    assert stats["per_list"][0]["list_title"] == "Servicefälle"


def test_stats_of_an_empty_zone(zone: LandingZone) -> None:
    stats = zone.stats()
    assert (stats["lists"], stats["items"], stats["files"]) == (0, 0, 0)
