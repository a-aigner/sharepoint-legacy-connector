"""The landing zone: a stable on-disk contract, not an implementation detail.

Layout (see README §Landing zone)::

    landing/
    ├── _manifest.json  _state.json  _graph.json  _graph.mmd  webs.json
    └── webs/{web_slug}/web.json
                       /lists/{list_guid}/list.json
                                          items.jsonl
                                          items_raw.jsonl
                                          files/{item_id}/{filename}

JSONL is written line-by-line and flushed per line. Some of these lists have
six figures of rows; accumulating them in memory to "write at the end" would
mean losing hours of crawling to one OOM.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any
from urllib.parse import urlsplit

from .config import get_logger
from .decode import json_default
from .models import ItemRecord, ListSchema, LookupGraph, Manifest, WebRef, guid_slug, normalise_url
from .schema import render_dot, render_mermaid

log = get_logger(__name__)

_SLUG_UNSAFE = re.compile(r"[^0-9a-zA-Z._-]+")


def web_slug(web_url: str) -> str:
    """``http://sp/sites/service/cases2008`` -> ``sites_sites_service_cases2008``-ish.

    Deterministic and collision-free enough in practice: host plus full path,
    with every unsafe character folded to ``_``.
    """
    parts = urlsplit(normalise_url(web_url))
    raw = (parts.netloc + parts.path) or web_url
    slug = _SLUG_UNSAFE.sub("_", raw).strip("_")
    return (slug or "root")[:150]


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=json_default)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


#: ``items.jsonl`` stores :class:`ItemRecord`; ``items_raw.jsonl`` stores the raw
#: row with the ``ows_`` prefix already stripped. Accept both spellings so the
#: two files stay in lockstep during truncation and delete.
DECODED_ID_KEYS = ("item_id",)
RAW_ID_KEYS = ("ID", "ows_ID", "Id")


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json") if hasattr(model, "model_dump") else model


class ListWriter:
    """Append-only writer for one list's two JSONL files."""

    def __init__(self, list_dir: Path) -> None:
        self.list_dir = list_dir
        self.items_path = list_dir / "items.jsonl"
        self.items_raw_path = list_dir / "items_raw.jsonl"
        self.files_dir = list_dir / "files"
        self._items: IO[str] | None = None
        self._raw: IO[str] | None = None
        self.lines_written = 0

    def open(self) -> ListWriter:
        self.list_dir.mkdir(parents=True, exist_ok=True)
        self._items = self.items_path.open("a", encoding="utf-8")
        self._raw = self.items_raw_path.open("a", encoding="utf-8")
        return self

    def __enter__(self) -> ListWriter:
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        for handle in (self._items, self._raw):
            if handle is not None:
                handle.flush()
                handle.close()
        self._items = None
        self._raw = None

    def write(self, record: ItemRecord, raw: dict[str, str]) -> None:
        if self._items is None or self._raw is None:  # pragma: no cover - misuse guard
            raise RuntimeError("ListWriter.write() before open()")
        payload = json.dumps(record.model_dump(mode="json"), ensure_ascii=False, default=json_default)
        self._items.write(payload)
        self._items.write("\n")
        self._items.flush()
        self._raw.write(json.dumps(raw, ensure_ascii=False))
        self._raw.write("\n")
        self._raw.flush()
        self.lines_written += 1

    def item_files_dir(self, item_id: int | str) -> Path:
        return self.files_dir / str(item_id)

    def reset(self) -> None:
        """Start this list from scratch (a full crawl without ``--resume``)."""
        self.close()
        self.items_path.unlink(missing_ok=True)
        self.items_raw_path.unlink(missing_ok=True)
        self.lines_written = 0

    # ---- resume support ----

    def truncate_after(self, last_item_id: int) -> int:
        """Drop rows beyond the last checkpoint, plus any half-written trailing line.

        Called before appending on ``--resume``. Guarantees the invariant the
        acceptance criteria state: no duplicates and no gaps.
        """
        removed = _truncate_jsonl(self.items_path, last_item_id, DECODED_ID_KEYS)
        removed += _truncate_jsonl(self.items_raw_path, last_item_id, RAW_ID_KEYS)
        if removed:
            log.info("resume.truncated", list_dir=str(self.list_dir), lines_removed=removed)
        return removed

    def delete_items(self, item_ids: set[int]) -> int:
        """Rewrite both JSONL files without the given ids (incremental deletes)."""
        if not item_ids:
            return 0
        removed = _filter_jsonl(self.items_path, item_ids, DECODED_ID_KEYS)
        removed += _filter_jsonl(self.items_raw_path, item_ids, RAW_ID_KEYS)
        return removed

    def existing_item_ids(self) -> set[int]:
        return set(_iter_ids(self.items_path, DECODED_ID_KEYS))


def _extract_id(record: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = record.get(key)
        if value is None:
            continue
        text = str(value).split(";#")[0].strip()
        try:
            return int(text)
        except ValueError:
            continue
    return None


def _iter_ids(path: Path, keys: tuple[str, ...]) -> Iterator[int]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            item_id = _extract_id(record, keys)
            if item_id is not None:
                yield item_id


def _rewrite(path: Path, keep: list[str]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for line in keep:
            handle.write(line)
            handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _truncate_jsonl(path: Path, last_item_id: int, keys: tuple[str, ...]) -> int:
    """Keep only well-formed lines whose id is <= ``last_item_id``."""
    if not path.exists():
        return 0
    keep: list[str] = []
    removed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                removed += 1  # partial trailing line from a killed process
                continue
            item_id = _extract_id(record, keys)
            if item_id is None or item_id > last_item_id:
                removed += 1
                continue
            keep.append(stripped)
    if removed:
        _rewrite(path, keep)
    return removed


def _filter_jsonl(path: Path, drop_ids: set[int], keys: tuple[str, ...]) -> int:
    if not path.exists():
        return 0
    keep: list[str] = []
    removed = 0
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                removed += 1
                continue
            item_id = _extract_id(record, keys)
            if item_id is not None and item_id in drop_ids:
                removed += 1
                continue
            keep.append(stripped)
    if removed:
        _rewrite(path, keep)
    return removed


class LandingZone:
    """Owns every path under ``landing/``."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    # ---- paths ----

    @property
    def manifest_path(self) -> Path:
        return self.root / "_manifest.json"

    @property
    def graph_json_path(self) -> Path:
        return self.root / "_graph.json"

    @property
    def graph_mmd_path(self) -> Path:
        return self.root / "_graph.mmd"

    @property
    def graph_dot_path(self) -> Path:
        return self.root / "_graph.dot"

    @property
    def webs_path(self) -> Path:
        return self.root / "webs.json"

    def web_dir(self, web_url: str) -> Path:
        return self.root / "webs" / web_slug(web_url)

    def list_dir(self, web_url: str, list_guid: str) -> Path:
        return self.web_dir(web_url) / "lists" / guid_slug(list_guid)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- writers ----

    def write_webs(self, webs: list[WebRef], lists_by_web: dict[str, list[Any]] | None = None) -> None:
        payload = []
        for web in webs:
            entry: dict[str, Any] = {
                "web_id": web.web_id,
                "title": web.title,
                "url": web.url,
                "slug": web_slug(web.url),
            }
            if lists_by_web is not None:
                entry["lists"] = [_dump(li) for li in lists_by_web.get(web.url, [])]
                entry["list_count"] = len(entry["lists"])
            payload.append(entry)
        write_json_atomic(self.webs_path, {"count": len(payload), "webs": payload})

    def write_web(self, web: WebRef, lists: list[Any]) -> None:
        write_json_atomic(
            self.web_dir(web.url) / "web.json",
            {
                "web_id": web.web_id,
                "title": web.title,
                "url": web.url,
                "slug": web_slug(web.url),
                "list_count": len(lists),
                "lists": [_dump(li) for li in lists],
            },
        )

    def write_list_schema(self, schema: ListSchema) -> Path:
        path = self.list_dir(schema.list_info.web_url, schema.list_info.guid) / "list.json"
        write_json_atomic(path, schema.model_dump(mode="json"))
        return path

    def read_list_schema(self, web_url: str, list_guid: str) -> ListSchema | None:
        path = self.list_dir(web_url, list_guid) / "list.json"
        if not path.exists():
            return None
        try:
            return ListSchema.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            log.warning("landing.schema_unreadable", path=str(path))
            return None

    def iter_list_schemas(self) -> Iterator[ListSchema]:
        for path in sorted(self.root.glob("webs/*/lists/*/list.json")):
            try:
                yield ListSchema.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                log.warning("landing.schema_unreadable", path=str(path))

    def write_graph(self, graph: LookupGraph) -> None:
        write_json_atomic(self.graph_json_path, graph.model_dump(mode="json"))
        self.graph_mmd_path.write_text(render_mermaid(graph), encoding="utf-8")

    def write_graph_dot(self, graph: LookupGraph) -> None:
        self.graph_dot_path.write_text(render_dot(graph), encoding="utf-8")

    def write_manifest(self, manifest: Manifest) -> None:
        write_json_atomic(self.manifest_path, manifest.model_dump(mode="json"))

    def read_manifest(self) -> Manifest | None:
        if not self.manifest_path.exists():
            return None
        try:
            return Manifest.model_validate_json(self.manifest_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def writer(self, web_url: str, list_guid: str) -> ListWriter:
        return ListWriter(self.list_dir(web_url, list_guid))

    # ---- stats ----

    def stats(self) -> dict[str, Any]:
        """Summarise what is actually on disk. Used by ``spconnect stats``."""
        lists = 0
        items = 0
        files = 0
        file_bytes = 0
        webs = 0

        if (self.root / "webs").exists():
            webs = len([p for p in (self.root / "webs").iterdir() if p.is_dir()])

        per_list: list[dict[str, Any]] = []
        for list_json in sorted(self.root.glob("webs/*/lists/*/list.json")):
            lists += 1
            list_dir = list_json.parent
            items_path = list_dir / "items.jsonl"
            count = sum(1 for _ in items_path.open(encoding="utf-8")) if items_path.exists() else 0
            items += count

            dir_files = 0
            dir_bytes = 0
            files_dir = list_dir / "files"
            if files_dir.exists():
                for path in files_dir.rglob("*"):
                    if path.is_file():
                        dir_files += 1
                        dir_bytes += path.stat().st_size
            files += dir_files
            file_bytes += dir_bytes

            title = ""
            with contextlib.suppress(Exception):
                title = json.loads(list_json.read_text(encoding="utf-8"))["list_info"]["title"]
            per_list.append(
                {
                    "list_title": title,
                    "path": str(list_dir.relative_to(self.root)),
                    "items": count,
                    "files": dir_files,
                    "file_bytes": dir_bytes,
                }
            )

        return {
            "root": str(self.root),
            "webs": webs,
            "lists": lists,
            "items": items,
            "files": files,
            "file_bytes": file_bytes,
            "per_list": per_list,
        }
