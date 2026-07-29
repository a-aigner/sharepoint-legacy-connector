"""Crawl orchestration: discovery, schema, items, files, incremental sync.

Policy that matters more than the code:

* A failure in one list must not abort the crawl. It is recorded in the
  manifest, the list is marked ``failed``, and the next list starts.
* Authentication failures are the exception — one bad credential producing 87
  "failed" lists is a useless artifact, so 401/403 aborts immediately.
* State is written after every page, so any interruption is resumable.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from . import __version__
from .config import Settings, get_logger
from .decode import RowDecoder, coerce_item_id, decode_attachment_urls, decode_attachments, strip_ows
from .files import FileDownloader, filename_from_url, sanitise_filename
from .landing import LandingZone, ListWriter
from .models import (
    AttachmentRecord,
    CrawlError,
    ItemRecord,
    ListInfo,
    ListSchema,
    LookupGraph,
    Manifest,
    WebRef,
    normalise_url,
    web_id_for,
)
from .schema import build_lookup_graph, viewfields_names
from .services.lists import ChangeBatch, ItemPage, ListsService, is_system_list
from .services.odata import ODataError, ODataRowMapper, ODataService
from .services.webs import WebsService
from .soap import SharePointSoapFault
from .state import StateStore, utcnow
from .transport import AuthenticationError, ServerVersion, Transport

log = get_logger(__name__)

MAX_SYNC_PAGES = 500

#: SharePoint 2010+ throttles queries that must examine more than this many rows.
#: Our ID-based paging is index-seekable and normally slips under it; recursive
#: queries over big document libraries are the ones that still trip.
LIST_VIEW_THRESHOLD = 5000

THRESHOLD_ADVICE = (
    "SharePoint 2010 throttles list queries at {threshold} items. spconnect pages on the "
    "indexed ID column, which normally avoids this. If a list still fails, ask the farm "
    "admin to raise the threshold, to add an index, or to schedule the crawl inside the "
    "daily unthrottled window."
)


class CrawlAborted(Exception):
    """Unrecoverable: bad credentials. Everything else is per-list."""


@dataclass
class RunReport:
    """Everything the final summary and ``_manifest.json`` need."""

    started_at: datetime = field(default_factory=utcnow)
    webs_discovered: int = 0
    web_discovery_method: str = "GetAllSubWebCollection"
    lists_discovered: int = 0
    lists_in_scope: int = 0
    lists_succeeded: int = 0
    lists_failed: int = 0
    lists_skipped: int = 0
    items_written: int = 0
    items_deleted: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    file_bytes: int = 0
    decoder_warnings: int = 0
    dangling_edges: int = 0
    errors: list[CrawlError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unique_scope_lists: list[str] = field(default_factory=list)
    large_lists: list[str] = field(default_factory=list)
    throttled_lists: list[str] = field(default_factory=list)
    odata_fallbacks: list[str] = field(default_factory=list)
    skip_reasons: dict[str, int] = field(default_factory=dict)

    def record_error(self, error: CrawlError) -> None:
        self.errors.append(error)

    def record_skip(self, reason: str) -> None:
        key = reason.split(":", 1)[0]
        self.skip_reasons[key] = self.skip_reasons.get(key, 0) + 1
        self.files_skipped += 1

    def counts(self) -> dict[str, int]:
        return {
            "webs_discovered": self.webs_discovered,
            "lists_discovered": self.lists_discovered,
            "lists_in_scope": self.lists_in_scope,
            "lists_succeeded": self.lists_succeeded,
            "lists_failed": self.lists_failed,
            "lists_skipped": self.lists_skipped,
            "items_written": self.items_written,
            "items_deleted": self.items_deleted,
            "files_downloaded": self.files_downloaded,
            "files_skipped": self.files_skipped,
            "file_bytes": self.file_bytes,
            "decoder_warnings": self.decoder_warnings,
            "dangling_edges": self.dangling_edges,
        }


def _matches(value: str, patterns: Sequence[str]) -> bool:
    """Case-insensitive substring match against any pattern."""
    lowered = value.lower()
    return any(p.lower() in lowered for p in patterns)


def _server_root(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def display_url_for(list_info: ListInfo, web_url: str, item_id: int) -> str:
    """Deep link back into SharePoint.

    The downstream RAG answers have to cite into the old system — that is what
    will make people trust the new one.
    """
    root = _server_root(web_url)
    view = list_info.default_view_url or ""
    if view:
        folder = view.rsplit("/", 1)[0]
    elif list_info.root_folder:
        folder = list_info.root_folder
        if not folder.startswith("/"):
            folder = f"{urlsplit(web_url).path}/{folder}"
        if list_info.is_document_library:
            folder = f"{folder}/Forms"
    else:
        return f"{web_url}/DispForm.aspx?ID={item_id}"
    return f"{root}{quote(folder, safe='/')}/DispForm.aspx?ID={item_id}"


class Crawler:
    """Owns one run. Not reused across commands."""

    def __init__(
        self,
        settings: Settings,
        transport: Transport,
        landing: LandingZone | None = None,
        state: StateStore | None = None,
        report: RunReport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self.landing = landing or LandingZone(settings.landing_dir)
        self.state = state or StateStore(settings.state_file)
        self.report = report or RunReport()
        self.downloader = FileDownloader(transport, settings)
        self._lists_services: dict[str, ListsService] = {}
        self._odata_services: dict[str, ODataService] = {}
        self.server_version: ServerVersion | None = transport.server_version

    # ---- services ----

    def lists_service(self, web_url: str) -> ListsService:
        key = normalise_url(web_url)
        service = self._lists_services.get(key)
        if service is None:
            service = ListsService(self.transport, key)
            self._lists_services[key] = service
        return service

    def ensure_version(self) -> ServerVersion:
        if self.server_version is None:
            self.server_version = self.transport.probe_version()
        return self.server_version

    # ---- scope ----

    def web_in_scope(self, web: WebRef) -> bool:
        include = self.settings.include_webs_list
        exclude = self.settings.exclude_webs_list
        if include and not _matches(web.url, include) and not _matches(web.title, include):
            return False
        return not (exclude and (_matches(web.url, exclude) or _matches(web.title, exclude)))

    def list_in_scope(self, list_info: ListInfo) -> tuple[bool, str | None]:
        if is_system_list(list_info):
            return False, "system_list"
        if list_info.hidden and not self.settings.include_hidden_lists:
            return False, "hidden"
        if list_info.is_document_library and not self.settings.include_document_libraries:
            return False, "document_library_excluded"
        include = self.settings.include_lists_list
        exclude = self.settings.exclude_lists_list
        if include and not (_matches(list_info.title, include) or _matches(list_info.guid, include)):
            return False, "not_in_include_lists"
        if exclude and (_matches(list_info.title, exclude) or _matches(list_info.guid, exclude)):
            return False, "excluded"
        return True, None

    # ---- 1. discovery ----

    def discover(self) -> tuple[list[WebRef], dict[str, list[ListInfo]]]:
        """Enumerate webs and their in-scope lists. Writes ``webs.json``."""
        version = self.ensure_version()
        webs_service = WebsService(self.transport, self.settings.base_url)
        discovery = webs_service.discover_all_webs(
            prefer_recursive_call=version.supports_all_sub_web_collection
        )
        all_webs = discovery.webs
        self.report.webs_discovered = len(all_webs)
        self.report.web_discovery_method = discovery.method
        self.report.warnings.extend(discovery.warnings)
        for url in discovery.unreadable:
            self.report.warnings.append(f"could not enumerate subwebs of {url}; some webs may be missing")

        webs = [w for w in all_webs if self.web_in_scope(w)]
        log.info(
            "discover.webs",
            discovered=len(all_webs),
            in_scope=len(webs),
            method=discovery.method,
            detail="web discovery returns only what this credential can read",
        )

        lists_by_web: dict[str, list[ListInfo]] = {}
        for web in webs:
            try:
                found = self.lists_service(web.url).get_list_collection()
            except AuthenticationError:
                raise
            except Exception as exc:
                self._record_error("discover", exc, web_url=web.url, operation="GetListCollection")
                lists_by_web[web.url] = []
                continue

            self.report.lists_discovered += len(found)
            in_scope: list[ListInfo] = []
            for list_info in found:
                ok, reason = self.list_in_scope(list_info)
                if not ok:
                    log.debug("discover.list_skipped", list=list_info.title, reason=reason)
                    continue
                if list_info.has_unique_scopes:
                    self.report.unique_scope_lists.append(f"{web.url} :: {list_info.title}")
                if version.has_list_view_threshold and list_info.item_count > LIST_VIEW_THRESHOLD:
                    self.report.large_lists.append(
                        f"{web.url} :: {list_info.title} ({list_info.item_count:,} items)"
                    )
                in_scope.append(list_info)
            lists_by_web[web.url] = in_scope
            self.landing.write_web(web, in_scope)

        if self.report.large_lists:
            self.report.warnings.append(
                f"{len(self.report.large_lists)} list(s) exceed the {LIST_VIEW_THRESHOLD}-item "
                "SharePoint 2010 list view threshold; see large_lists"
            )
        self.report.lists_in_scope = sum(len(v) for v in lists_by_web.values())
        self.landing.ensure()
        self.landing.write_webs(webs, lists_by_web)
        return webs, lists_by_web

    # ---- 2. schema ----

    def fetch_schemas(
        self, lists_by_web: dict[str, list[ListInfo]], use_cache: bool = False
    ) -> list[ListSchema]:
        schemas: list[ListSchema] = []
        for web_url, lists in lists_by_web.items():
            service = self.lists_service(web_url)
            for list_info in lists:
                if use_cache:
                    cached = self.landing.read_list_schema(web_url, list_info.guid)
                    if cached is not None:
                        schemas.append(cached)
                        continue
                try:
                    schema = service.get_list_schema(list_info)
                except AuthenticationError:
                    raise
                except Exception as exc:
                    self._record_error(
                        "schema", exc, web_url=web_url, list_info=list_info, operation="GetList"
                    )
                    continue
                schema.fetched_at = utcnow()
                self.landing.write_list_schema(schema)
                schemas.append(schema)
                log.info(
                    "schema.fetched",
                    web=web_url,
                    list=list_info.title,
                    fields=len(schema.fields),
                    lookups=sum(1 for f in schema.fields if f.is_lookup),
                )
        return schemas

    def build_graph(self, schemas: Iterable[ListSchema]) -> LookupGraph:
        graph = build_lookup_graph(schemas)
        self.report.dangling_edges = len(graph.dangling_edges)
        self.landing.write_graph(graph)
        return graph

    # ---- item sources ----

    def odata_service(self, web_url: str) -> ODataService:
        key = normalise_url(web_url)
        service = self._odata_services.get(key)
        if service is None:
            service = ODataService(self.transport, key)
            self._odata_services[key] = service
        return service

    def _odata_page(
        self,
        list_info: ListInfo,
        schema: ListSchema,
        entity_set: str,
        last_id: int,
        mapper: ODataRowMapper,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
        """One OData page, returned as ``(raw_rows, decoded_rows, max_id)``."""
        service = self.odata_service(list_info.web_url)
        expand = (
            [f.name for f in schema.fields if f.is_lookup][:12]
            if self.settings.odata_expand_lookups
            else None
        )
        try:
            page = service.get_items(entity_set, last_id=last_id, top=self.settings.page_size, expand=expand)
        except ODataError:
            if not expand:
                raise
            # Wide $expand is a known way to make a 2010 farm return 500.
            log.warning("odata.expand_failed", list=list_info.title, detail="retrying without $expand")
            page = service.get_items(entity_set, last_id=last_id, top=self.settings.page_size)

        raw_rows: list[dict[str, Any]] = []
        decoded_rows: list[dict[str, Any]] = []
        for entity in page.rows:
            raw, decoded = mapper.map_row(entity)
            raw_rows.append(raw)
            decoded_rows.append(decoded)
        return raw_rows, decoded_rows, page.max_id

    # ---- 3. items ----

    def crawl_list(self, list_info: ListInfo, schema: ListSchema, resume: bool = False) -> int:
        """Full ID-paged pull of one list. Returns the number of items written."""
        guid = list_info.guid
        entry = self.state.get(guid)
        writer = self.landing.writer(list_info.web_url, guid)

        if resume and entry.status == "complete":
            log.info("crawl.list_already_complete", list=list_info.title, items=entry.items_written)
            self.report.lists_skipped += 1
            return 0

        if resume and entry.last_item_id:
            writer.truncate_after(entry.last_item_id)
            last_id = entry.last_item_id
            written = entry.items_written
        else:
            writer.reset()
            last_id = 0
            written = 0

        self.state.update(
            guid,
            web_url=list_info.web_url,
            list_title=list_info.title,
            status="in_progress",
            last_item_id=last_id,
            items_written=written,
            error=None,
        )
        self.state.save()

        decoder = RowDecoder(
            schema.field_map(),
            list_title=list_info.title,
            list_guid=guid,
            web_url=list_info.web_url,
        )
        field_names = viewfields_names(schema.fields)
        service = self.lists_service(list_info.web_url)
        display_names = schema.display_names()

        entity_set: str | None = None
        mapper: ODataRowMapper | None = None
        if self.settings.api_mode == "odata":
            entity_set = self.odata_service(list_info.web_url).entity_set_for(list_info.title)
            if entity_set is None:
                # No entity set means no REST path for this list. Falling back is
                # strictly better than skipping it.
                self.report.warnings.append(
                    f"{list_info.title}: no ListData.svc entity set found; used SOAP for this list"
                )
                self.report.odata_fallbacks.append(f"{list_info.web_url} :: {list_info.title}")
            else:
                mapper = ODataRowMapper(schema)

        writer.open()
        try:
            page_number = 0
            while True:
                if entity_set is not None and mapper is not None:
                    raw_rows, decoded_rows, max_id = self._odata_page(
                        list_info, schema, entity_set, last_id, mapper
                    )
                    rows = raw_rows
                else:
                    page: ItemPage = service.get_list_items(
                        guid,
                        last_id=last_id,
                        row_limit=self.settings.page_size,
                        field_names=field_names,
                    )
                    rows = page.rows
                    decoded_rows = []
                    max_id = page.max_id
                page_number += 1
                if not rows:
                    break

                if max_id is None or max_id <= last_id:
                    raise RuntimeError(
                        f"paging stalled on '{list_info.title}': page {page_number} returned "
                        f"{len(rows)} rows with max ID {max_id} <= last ID {last_id}"
                    )

                written += self._write_page(
                    writer,
                    rows,
                    list_info,
                    decoder,
                    display_names,
                    decoded_rows if entity_set is not None else None,
                )
                last_id = max_id
                self.state.update(guid, last_item_id=last_id, items_written=written, status="in_progress")
                self.state.save()

                total = list_info.item_count or 0
                percent = f"{100 * written / total:.0f}%" if total else "?"
                log.info(
                    "crawl.page",
                    list=list_info.title,
                    page=page_number,
                    rows=len(rows),
                    progress=f"{written:,}/{total:,} ({percent})" if total else f"{written:,}",
                    last_id=last_id,
                    source="odata" if entity_set else "soap",
                )

                if len(rows) < self.settings.page_size:
                    break
        finally:
            writer.close()
            self.report.decoder_warnings += decoder.warning_count

        self.state.update(
            guid,
            status="complete",
            last_full_crawl=utcnow(),
            items_written=written,
            last_item_id=last_id,
            error=None,
        )
        self.state.save()
        return written

    def _write_page(
        self,
        writer: ListWriter,
        rows: list[dict[str, Any]],
        list_info: ListInfo,
        decoder: RowDecoder,
        display_names: dict[str, str],
        decoded_rows: list[dict[str, Any]] | None = None,
    ) -> int:
        """Write one page. ``decoded_rows`` short-circuits the ``ows_`` decoder
        for the OData backend, which delivers typed values already."""
        written = 0
        for index, raw in enumerate(rows):
            item_id = coerce_item_id(raw)
            if item_id is None:
                log.warning("crawl.row_without_id", list=list_info.title)
                continue
            decoded = decoded_rows[index] if decoded_rows is not None else decoder.decode_row(raw)
            log.debug(
                "crawl.item",
                list=list_info.title,
                item_id=item_id,
                fields=len(decoded),
                title=decoded.get("Title"),
            )
            record = self._build_record(list_info, item_id, decoded, display_names)
            record.attachments = self._collect_files(writer, list_info, item_id, raw, decoded)
            writer.write(record, strip_ows(raw) if decoded_rows is None else raw)
            written += 1
            self.report.items_written += 1
        return written

    def _build_record(
        self,
        list_info: ListInfo,
        item_id: int,
        decoded: dict[str, Any],
        display_names: dict[str, str],
    ) -> ItemRecord:
        def as_text(key: str) -> str | None:
            value = decoded.get(key)
            if isinstance(value, dict):
                return value.get("value")
            return value if isinstance(value, str) else None

        def as_iso(key: str) -> str | None:
            value = decoded.get(key)
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%dT%H:%M:%SZ")
            return value if isinstance(value, str) else None

        web_url = list_info.web_url
        return ItemRecord(
            doc_id=f"{web_id_for(web_url)}:{list_info.guid}:{item_id}",
            web_url=web_url,
            web_id=web_id_for(web_url),
            list_guid=list_info.guid,
            list_title=list_info.title,
            item_id=item_id,
            display_url=display_url_for(list_info, web_url, item_id),
            content_type=as_text("ContentType"),
            created=as_iso("Created"),
            modified=as_iso("Modified"),
            is_folder=decoded.get("FSObjType") == 1,
            file_ref=as_text("FileRef"),
            file_name=as_text("FileLeafRef"),
            fields=decoded,
            field_display_names=display_names,
        )

    # ---- 4. files ----

    def _collect_files(
        self,
        writer: ListWriter,
        list_info: ListInfo,
        item_id: int,
        raw: dict[str, str],
        decoded: dict[str, Any],
    ) -> list[AttachmentRecord]:
        targets: list[tuple[str, str | None]] = []

        if list_info.is_document_library:
            if decoded.get("FSObjType") != 1:  # folders have no bytes
                url = raw.get("ows_EncodedAbsUrl")
                if url:
                    name = raw.get("ows_FileLeafRef", "")
                    targets.append((url, sanitise_filename(name.split(";#")[-1]) if name else None))
        else:
            urls = decoded.get("AttachmentUrls") or decode_attachment_urls(raw.get("ows_AttachmentUrls"))
            if not urls and decode_attachments(raw.get("ows_Attachments", "0")):
                # IncludeAttachmentUrls did not populate; pay for the round trip.
                try:
                    urls = self.lists_service(list_info.web_url).get_attachment_collection(
                        list_info.guid, item_id
                    )
                except AuthenticationError:
                    raise
                except Exception as exc:
                    log.warning(
                        "crawl.attachment_collection_failed",
                        list=list_info.title,
                        item_id=item_id,
                        error=str(exc),
                    )
                    urls = []
            targets.extend((url, None) for url in urls)

        if not targets:
            return []

        if not self.settings.download_files:
            return [
                AttachmentRecord(
                    filename=name or filename_from_url(url), url=url, skip_reason="downloads_disabled"
                )
                for url, name in targets
            ]

        dest = writer.item_files_dir(item_id)
        workers = max(1, min(self.settings.concurrency, len(targets)))
        if workers == 1:
            records = [self.downloader.download(url, dest, name) for url, name in targets]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                records = list(pool.map(lambda t: self.downloader.download(t[0], dest, t[1]), targets))

        for record in records:
            if record.downloaded:
                self.report.files_downloaded += 1
                self.report.file_bytes += record.bytes or 0
                if record.local_path:
                    record.local_path = str(Path(record.local_path).relative_to(writer.list_dir))
            elif record.skip_reason:
                self.report.record_skip(record.skip_reason)
        return records

    # ---- 5. full crawl ----

    def crawl(self, resume: bool = False) -> RunReport:
        webs, lists_by_web = self.discover()
        schemas = self.fetch_schemas(lists_by_web, use_cache=resume)
        self.build_graph(schemas)

        schema_by_guid = {s.list_info.guid: s for s in schemas}
        total = sum(len(v) for v in lists_by_web.values())
        index = 0

        for web_url, lists in lists_by_web.items():
            for list_info in lists:
                index += 1
                schema = schema_by_guid.get(list_info.guid)
                if schema is None:
                    self.report.lists_failed += 1
                    continue
                log.info(
                    "crawl.list",
                    progress=f"{index}/{total}",
                    web=web_url,
                    list=list_info.title,
                    expected_items=list_info.item_count,
                )
                try:
                    self.crawl_list(schema.list_info, schema, resume=resume)
                    self.report.lists_succeeded += 1
                except AuthenticationError as exc:
                    raise CrawlAborted(str(exc)) from exc
                except Exception as exc:
                    self.report.lists_failed += 1
                    if isinstance(exc, SharePointSoapFault) and exc.is_list_view_threshold:
                        self.report.throttled_lists.append(f"{web_url} :: {list_info.title}")
                    self.state.update(list_info.guid, status="failed", error=str(exc))
                    self.state.save()
                    self._record_error(
                        "crawl", exc, web_url=web_url, list_info=list_info, operation="GetListItems"
                    )
        _ = webs
        return self.report

    # ---- 6. incremental sync ----

    def sync(self) -> RunReport:
        version = self.ensure_version()
        webs, lists_by_web = self.discover()
        schemas = self.fetch_schemas(lists_by_web, use_cache=True)
        self.build_graph(schemas)
        schema_by_guid = {s.list_info.guid: s for s in schemas}

        total = sum(len(v) for v in lists_by_web.values())
        index = 0
        for web_url, lists in lists_by_web.items():
            for list_info in lists:
                index += 1
                schema = schema_by_guid.get(list_info.guid)
                if schema is None:
                    self.report.lists_failed += 1
                    continue
                log.info("sync.list", progress=f"{index}/{total}", web=web_url, list=list_info.title)
                try:
                    self.sync_list(schema.list_info, schema, version)
                    self.report.lists_succeeded += 1
                except AuthenticationError as exc:
                    raise CrawlAborted(str(exc)) from exc
                except Exception as exc:
                    self.report.lists_failed += 1
                    self.state.update(list_info.guid, status="failed", error=str(exc))
                    self.state.save()
                    self._record_error(
                        "sync",
                        exc,
                        web_url=web_url,
                        list_info=list_info,
                        operation="GetListItemChangesSinceToken",
                    )
        _ = webs
        return self.report

    def sync_list(self, list_info: ListInfo, schema: ListSchema, version: ServerVersion) -> int:
        guid = list_info.guid
        entry = self.state.get(guid)

        if not version.supports_change_tokens:
            self.report.warnings.append(
                f"{list_info.title}: server major version {version.major} predates change tokens; full crawl"
            )
            return self.crawl_list(list_info, schema, resume=False)

        if not entry.change_token or entry.status != "complete":
            log.info("sync.no_token", list=list_info.title, detail="falling back to a full crawl")
            written = self.crawl_list(list_info, schema, resume=False)
            self._prime_change_token(list_info, schema)
            return written

        service = self.lists_service(list_info.web_url)
        field_names = viewfields_names(schema.fields)
        writer = self.landing.writer(list_info.web_url, guid)
        decoder = RowDecoder(
            schema.field_map(),
            list_title=list_info.title,
            list_guid=guid,
            web_url=list_info.web_url,
        )
        display_names = schema.display_names()

        token: str | None = entry.change_token
        changed_rows: list[dict[str, str]] = []
        deleted: set[int] = set()

        for _ in range(MAX_SYNC_PAGES):
            try:
                batch: ChangeBatch = service.get_list_item_changes_since_token(
                    guid, change_token=token, row_limit=self.settings.page_size, field_names=field_names
                )
            except SharePointSoapFault as exc:
                log.warning("sync.token_rejected", list=list_info.title, error=str(exc))
                self.report.warnings.append(f"{list_info.title}: change token rejected, full crawl")
                self.state.update(guid, change_token=None)
                written = self.crawl_list(list_info, schema, resume=False)
                self._prime_change_token(list_info, schema)
                return written

            if batch.invalid_token:
                log.warning("sync.invalid_token", list=list_info.title)
                self.report.warnings.append(f"{list_info.title}: change token invalid, full crawl")
                self.state.update(guid, change_token=None)
                written = self.crawl_list(list_info, schema, resume=False)
                self._prime_change_token(list_info, schema)
                return written

            changed_rows.extend(batch.rows)
            deleted.update(batch.deleted_ids)
            if batch.last_change_token:
                token = batch.last_change_token
            if not batch.more_changes:
                break

        changed_ids = {i for i in (coerce_item_id(r) for r in changed_rows) if i is not None}
        # A vector DB full of cases that no longer exist is worse than a stale one.
        removed = writer.delete_items(changed_ids | deleted)
        self.report.items_deleted += len(deleted)

        writer.open()
        try:
            written = self._write_page(writer, changed_rows, list_info, decoder, display_names)
        finally:
            writer.close()
            self.report.decoder_warnings += decoder.warning_count

        last_id = max([entry.last_item_id, *changed_ids])
        self.state.update(
            guid,
            change_token=token,
            last_sync=utcnow(),
            status="complete",
            last_item_id=last_id,
            items_written=max(0, entry.items_written - removed + written),
            error=None,
        )
        self.state.save()
        log.info(
            "sync.applied",
            list=list_info.title,
            updated=written,
            deleted=len(deleted),
            lines_replaced=removed,
        )
        return written

    def _prime_change_token(self, list_info: ListInfo, schema: ListSchema) -> None:
        """After a full crawl, grab a token so the *next* sync can be incremental."""
        version = self.ensure_version()
        if not version.supports_change_tokens:
            return
        try:
            batch = self.lists_service(list_info.web_url).get_list_item_changes_since_token(
                list_info.guid, change_token=None, row_limit=1, field_names=["ID"]
            )
        except Exception as exc:
            log.warning("sync.token_prime_failed", list=list_info.title, error=str(exc))
            return
        if batch.last_change_token:
            self.state.update(list_info.guid, change_token=batch.last_change_token)
            self.state.save()

    # ---- 7. dry run ----

    def dry_run(self) -> dict[str, Any]:
        """Read-only sizing pass: what *would* be crawled, and how many requests."""
        webs, lists_by_web = self.discover()
        rows: list[dict[str, Any]] = []
        total_requests = 0
        total_items = 0
        for web_url, lists in lists_by_web.items():
            for list_info in lists:
                pages = max(1, math.ceil((list_info.item_count or 0) / self.settings.page_size))
                requests = pages + 1  # + GetList for the schema
                if self.settings.download_files and (
                    list_info.is_document_library or list_info.enable_attachments
                ):
                    requests += list_info.item_count
                total_requests += requests
                total_items += list_info.item_count
                rows.append(
                    {
                        "web_url": web_url,
                        "list_title": list_info.title,
                        "list_guid": list_info.guid,
                        "base_type": list_info.base_type_name,
                        "items": list_info.item_count,
                        "pages": pages,
                        "estimated_requests": requests,
                        "has_unique_scopes": list_info.has_unique_scopes,
                    }
                )
        rate = self.settings.requests_per_second or 1
        return {
            "webs": len(webs),
            "lists": len(rows),
            "items": total_items,
            "estimated_requests": total_requests + len(webs) + 1,
            "estimated_minutes": round((total_requests + len(webs) + 1) / rate / 60, 1),
            "rows": rows,
        }

    # ---- 8. verify-time ----

    def verify_time(self, list_selector: str, item_id: int) -> dict[str, Any]:
        """Print raw vs decoded datetimes for one item so a human can compare.

        ``DateInUtc`` is a claim about this build's behaviour, not a guarantee.
        Somebody has to check it against the SharePoint UI exactly once.
        """
        _webs, lists_by_web = self.discover()
        target: ListInfo | None = None
        for lists in lists_by_web.values():
            for list_info in lists:
                if list_selector.lower() in (list_info.title.lower(), list_info.guid.lower()):
                    target = list_info
                    break
                if list_selector.lower() in list_info.title.lower():
                    target = target or list_info
            if target is not None:
                break
        if target is None:
            raise ValueError(f"no in-scope list matching {list_selector!r}")

        service = self.lists_service(target.web_url)
        schema = self.landing.read_list_schema(target.web_url, target.guid) or service.get_list_schema(target)
        page = service.get_list_items(
            target.guid,
            last_id=item_id - 1,
            row_limit=1,
            field_names=viewfields_names(schema.fields),
        )
        if not page.rows:
            raise ValueError(f"item {item_id} not found in {target.title!r}")
        raw = page.rows[0]

        decoder = RowDecoder(
            schema.field_map(), list_title=target.title, list_guid=target.guid, web_url=target.web_url
        )
        decoded = decoder.decode_row(raw)
        datetime_fields = {
            name: field.type for name, field in schema.field_map().items() if field.type == "DateTime"
        }
        datetime_fields.update({"Created": "DateTime", "Modified": "DateTime"})

        comparisons = []
        for name in sorted(datetime_fields):
            wire = raw.get(f"ows_{name}")
            if wire is None:
                continue
            value = decoded.get(name)
            comparisons.append(
                {
                    "field": name,
                    "display_name": schema.display_names().get(name, name),
                    "raw_wire_value": wire,
                    "decoded_utc": value.strftime("%Y-%m-%dT%H:%M:%SZ")
                    if isinstance(value, datetime)
                    else value,
                }
            )
        return {
            "list_title": target.title,
            "list_guid": target.guid,
            "web_url": target.web_url,
            "item_id": coerce_item_id(raw),
            "display_url": display_url_for(target, target.web_url, item_id),
            "query_options": "DateInUtc=TRUE",
            "fields": comparisons,
        }

    # ---- errors / manifest ----

    def _record_error(
        self,
        scope: str,
        exc: Exception,
        *,
        web_url: str | None = None,
        list_info: ListInfo | None = None,
        operation: str | None = None,
    ) -> None:
        error = CrawlError(
            when=utcnow(),
            scope=scope,
            web_url=web_url,
            list_guid=list_info.guid if list_info else None,
            list_title=list_info.title if list_info else None,
            operation=operation,
            error_type=type(exc).__name__,
            message=str(exc),
        )
        self.report.record_error(error)
        log.error(
            f"{scope}.failed",
            web=web_url,
            list=list_info.title if list_info else None,
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )

    def write_manifest(self, command: str) -> Manifest:
        version = self.server_version or self.transport.server_version
        manifest = Manifest(
            command=command,
            spconnect_version=__version__,
            started_at=self.report.started_at,
            finished_at=utcnow(),
            base_url=self.settings.base_url,
            server_version=version.as_dict() if version else {},
            web_discovery_method=self.report.web_discovery_method,
            api_mode=self.settings.api_mode,
            config=self.settings.redacted_dict(),
            counts=self.report.counts(),
            warnings=self.report.warnings,
            errors=self.report.errors,
            lists_with_unique_scopes=self.report.unique_scope_lists,
        )
        self.landing.ensure()
        self.landing.write_manifest(manifest)
        return manifest
