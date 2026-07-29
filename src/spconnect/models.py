"""Pydantic models for everything that reaches disk."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field

#: Namespace for deriving stable web ids from web URLs. None of the seven
#: permitted SOAP operations returns a web GUID, so ``doc_id`` uses a uuid5 of
#: the normalised web URL instead — deterministic, and stable across runs.
WEB_ID_NAMESPACE = uuid.NAMESPACE_URL

BASE_TYPE_NAMES = {
    "0": "generic_list",
    "1": "document_library",
    "3": "discussion_board",
    "4": "survey",
    "5": "issue_tracking",
}


def normalise_url(url: str) -> str:
    """Lowercase scheme+host, strip trailing slash, preserve path case."""
    parts = urlsplit(url.strip())
    if not parts.scheme:
        return url.strip().rstrip("/")
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def web_id_for(web_url: str) -> str:
    """Stable synthetic web GUID derived from the normalised URL."""
    return str(uuid.uuid5(WEB_ID_NAMESPACE, normalise_url(web_url)))


def normalise_guid(guid: str | None) -> str:
    """SharePoint returns braced GUIDs. Keep the braces, uppercase the hex."""
    if not guid:
        return ""
    g = guid.strip()
    if g.startswith("{") and g.endswith("}"):
        return "{" + g[1:-1].upper() + "}"
    return "{" + g.upper() + "}"


def guid_slug(guid: str) -> str:
    """Filesystem-safe form of a braced GUID."""
    return guid.strip("{}").lower()


class WebRef(BaseModel):
    """A site/subsite discovered via ``Webs.GetAllSubWebCollection``."""

    title: str = ""
    url: str
    web_id: str = ""

    def model_post_init(self, __context: Any) -> None:
        self.url = normalise_url(self.url)
        if not self.web_id:
            self.web_id = web_id_for(self.url)


class ListInfo(BaseModel):
    """A list as returned by ``Lists.GetListCollection``."""

    model_config = ConfigDict(populate_by_name=True)

    guid: str
    title: str = ""
    description: str = ""
    base_type: str = "0"
    server_template: str = ""
    item_count: int = 0
    hidden: bool = False
    root_folder: str = ""
    default_view_url: str = ""
    enable_attachments: bool = False
    has_unique_scopes: bool = False
    created: str | None = None
    modified: str | None = None
    web_url: str = ""
    raw_attributes: dict[str, str] = Field(default_factory=dict)

    @property
    def is_document_library(self) -> bool:
        return self.base_type == "1"

    @property
    def base_type_name(self) -> str:
        return BASE_TYPE_NAMES.get(self.base_type, f"unknown_{self.base_type}")

    @property
    def slug(self) -> str:
        return guid_slug(self.guid)


class ChoiceOption(BaseModel):
    value: str


class FieldDef(BaseModel):
    """One column of a list, from ``Lists.GetList``."""

    id: str = ""
    name: str
    static_name: str = ""
    display_name: str = ""
    type: str = "Text"
    required: bool = False
    hidden: bool = False
    read_only: bool = False
    lookup_list: str | None = None
    show_field: str | None = None
    mult: bool = False
    col_name: str | None = None
    format: str | None = None
    result_type: str | None = None
    max_length: int | None = None
    choices: list[str] = Field(default_factory=list)
    formula: str | None = None
    raw_attributes: dict[str, str] = Field(default_factory=dict)

    @property
    def is_lookup(self) -> bool:
        return self.type in ("Lookup", "LookupMulti")

    @property
    def unescaped_name(self) -> str:
        from .schema import unescape_internal_name

        return unescape_internal_name(self.name)


class ListSchema(BaseModel):
    """``list.json`` — list metadata plus its full field schema."""

    list_info: ListInfo
    fields: list[FieldDef] = Field(default_factory=list)
    fetched_at: datetime | None = None

    def field_map(self) -> dict[str, FieldDef]:
        return {f.name: f for f in self.fields}

    def display_names(self) -> dict[str, str]:
        return {f.name: f.display_name or f.name for f in self.fields}


class LookupEdge(BaseModel):
    """One ``Lookup``/``LookupMulti`` column, i.e. one CRM foreign key."""

    source_list_guid: str
    source_list_title: str
    source_web_url: str
    target_list_guid: str
    target_list_title: str | None = None
    field_name: str
    field_display_name: str
    show_field: str | None = None
    multi: bool = False
    self_reference: bool = False
    dangling: bool = False


class GraphNode(BaseModel):
    list_guid: str
    title: str
    web_url: str
    item_count: int = 0
    base_type: str = "0"
    base_type_name: str = "generic_list"


class LookupGraph(BaseModel):
    """``_graph.json`` — the recovered data model of the CRM."""

    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[LookupEdge] = Field(default_factory=list)

    @property
    def dangling_edges(self) -> list[LookupEdge]:
        return [e for e in self.edges if e.dangling]


class AttachmentRecord(BaseModel):
    filename: str
    url: str
    local_path: str | None = None
    bytes: int | None = None
    sha256: str | None = None
    downloaded: bool = False
    skip_reason: str | None = None


class ItemRecord(BaseModel):
    """One line of ``items.jsonl``."""

    doc_id: str
    web_url: str
    web_id: str
    list_guid: str
    list_title: str
    item_id: int
    display_url: str
    content_type: str | None = None
    created: str | None = None
    modified: str | None = None
    is_folder: bool = False
    file_ref: str | None = None
    file_name: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    field_display_names: dict[str, str] = Field(default_factory=dict)
    attachments: list[AttachmentRecord] = Field(default_factory=list)


class CrawlError(BaseModel):
    when: datetime
    scope: str
    web_url: str | None = None
    list_guid: str | None = None
    list_title: str | None = None
    operation: str | None = None
    error_type: str
    message: str


class Manifest(BaseModel):
    """``_manifest.json``."""

    command: str
    spconnect_version: str
    started_at: datetime
    finished_at: datetime | None = None
    base_url: str = ""
    server_version: dict[str, Any] = Field(default_factory=dict)
    web_discovery_method: str = "GetAllSubWebCollection"
    api_mode: str = "soap"
    config: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[CrawlError] = Field(default_factory=list)
    lists_with_unique_scopes: list[str] = Field(default_factory=list)
