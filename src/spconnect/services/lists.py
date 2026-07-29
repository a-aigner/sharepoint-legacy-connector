"""``Lists.asmx`` — list discovery, schema, items, changes, attachments."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lxml import etree

from ..config import get_logger
from ..models import ListInfo, ListSchema, normalise_url
from ..schema import parse_fields, parse_list_attributes
from ..soap import SoapClient, element, find_all, find_one, text_content

log = get_logger(__name__)

#: System lists that are never business data. Extend freely.
SYSTEM_LIST_TITLES = frozenset(
    {
        "Master Page Gallery",
        "Web Part Gallery",
        "List Template Gallery",
        "Site Template Gallery",
        "User Information List",
        "Solution Gallery",
        "Style Library",
        "Form Templates",
        "Workflow History",
        "TaskAttachments",
        "Reporting Metadata",
        "Reporting Templates",
        "Converted Forms",
    }
)


def query_options(
    *,
    paging_token: str | None = None,
    include_attachment_urls: bool = True,
) -> etree._Element:
    """The query options used on every item call.

    ``DateInUtc`` is the important one: without it datetimes come back in
    server-local time in an ambiguous format, and there is no way to recover
    the intended instant afterwards.
    """
    children = [
        element("DateInUtc", text="TRUE"),
        element("IncludeMandatoryColumns", text="TRUE"),
        element("ViewAttributes", {"Scope": "RecursiveAll"}),
    ]
    if include_attachment_urls:
        children.insert(2, element("IncludeAttachmentUrls", text="TRUE"))
    if paging_token:
        children.append(element("Paging", {"ListItemCollectionPositionNext": paging_token}))
    return element("QueryOptions", children=children)


def id_page_query(last_id: int) -> etree._Element:
    """``ID > last_id ORDER BY ID`` — deterministic, resumable paging.

    Token paging works too, but the token is not resumable across process
    restarts and has to be re-escaped. Paging on the ID counter survives a
    crashed crawl, which on a server this slow matters more than elegance.
    """
    where = element(
        "Where",
        children=[
            element(
                "Gt",
                children=[
                    element("FieldRef", {"Name": "ID"}),
                    element("Value", {"Type": "Counter"}, text=str(last_id)),
                ],
            )
        ],
    )
    order_by = element("OrderBy", children=[element("FieldRef", {"Name": "ID", "Ascending": "TRUE"})])
    return element("Query", children=[where, order_by])


def view_fields(names: list[str] | None) -> etree._Element:
    """Explicit ``<ViewFields>``; ``Properties='TRUE'`` when we have no schema."""
    if not names:
        return element("ViewFields", {"Properties": "TRUE"})
    return element("ViewFields", children=[element("FieldRef", {"Name": n}) for n in names])


@dataclass
class ItemPage:
    """One page of ``<z:row>`` attribute dicts."""

    rows: list[dict[str, str]] = field(default_factory=list)
    item_count: int | None = None
    position_next: str | None = None

    @property
    def max_id(self) -> int | None:
        ids = []
        for row in self.rows:
            raw = row.get("ows_ID")
            if raw is None:
                continue
            try:
                ids.append(int(str(raw).split(";#")[0]))
            except ValueError:
                continue
        return max(ids) if ids else None


@dataclass
class ChangeBatch:
    """A ``GetListItemChangesSinceToken`` response."""

    rows: list[dict[str, str]] = field(default_factory=list)
    deleted_ids: list[int] = field(default_factory=list)
    last_change_token: str | None = None
    invalid_token: bool = False
    more_changes: bool = False


def _row_attributes(el: etree._Element) -> dict[str, str]:
    return {str(k): str(v) for k, v in el.attrib.items()}


class ListsService:
    """Wraps ``Lists.asmx`` for one web."""

    def __init__(self, transport: Any, web_url: str) -> None:
        self.client = SoapClient(transport, web_url, "Lists")
        self.web_url = normalise_url(web_url)

    # ---- 6.2 discovery ----

    def get_list_collection(self) -> list[ListInfo]:
        result = self.client.call("GetListCollection")
        lists = [parse_list_attributes(el, self.web_url) for el in find_all(result, "List")]
        log.info("lists.discovered", web=self.web_url, count=len(lists))
        return lists

    # ---- 6.3 schema ----

    def get_list(self, list_name: str) -> tuple[ListInfo, list[etree._Element]]:
        """``listName`` should be the braced GUID — more reliable than the title."""
        result = self.client.call("GetList", {"listName": list_name})
        list_el = find_one(result, "List")
        if list_el is None:
            raise ValueError(f"GetList({list_name}): no <List> element in response")
        return parse_list_attributes(list_el, self.web_url), [list_el]

    def get_list_schema(self, list_info: ListInfo) -> ListSchema:
        """Fetch the full field schema, keeping the discovery metadata."""
        fetched, elements = self.get_list(list_info.guid or list_info.title)
        merged = list_info.model_copy(
            update={
                "item_count": fetched.item_count or list_info.item_count,
                "enable_attachments": fetched.enable_attachments or list_info.enable_attachments,
                "has_unique_scopes": fetched.has_unique_scopes or list_info.has_unique_scopes,
                "root_folder": fetched.root_folder or list_info.root_folder,
                "default_view_url": fetched.default_view_url or list_info.default_view_url,
                "base_type": list_info.base_type or fetched.base_type,
                "web_url": self.web_url,
            }
        )
        return ListSchema(list_info=merged, fields=parse_fields(elements[0]))

    # ---- 6.4 items ----

    def get_list_items(
        self,
        list_name: str,
        *,
        last_id: int = 0,
        row_limit: int = 200,
        field_names: list[str] | None = None,
    ) -> ItemPage:
        result = self.client.call(
            "GetListItems",
            {
                "listName": list_name,
                "viewName": "",
                "query": id_page_query(last_id),
                "viewFields": view_fields(field_names),
                "rowLimit": str(row_limit),
                "queryOptions": query_options(),
                "webID": "",
            },
        )
        return self._parse_items(result)

    @staticmethod
    def _parse_items(result: etree._Element) -> ItemPage:
        page = ItemPage()
        data = find_one(result, "data")
        if data is not None:
            count = data.get("ItemCount")
            if count is not None:
                try:
                    page.item_count = int(count)
                except ValueError:
                    page.item_count = None
            page.position_next = data.get("ListItemCollectionPositionNext")
        page.rows = [_row_attributes(el) for el in find_all(result, "row")]
        return page

    # ---- 6.5 incremental ----

    def get_list_item_changes_since_token(
        self,
        list_name: str,
        *,
        change_token: str | None = None,
        row_limit: int = 200,
        field_names: list[str] | None = None,
    ) -> ChangeBatch:
        params: dict[str, Any] = {
            "listName": list_name,
            "viewName": "",
            "query": element(
                "Query",
                children=[
                    element("OrderBy", children=[element("FieldRef", {"Name": "ID", "Ascending": "TRUE"})])
                ],
            ),
            "viewFields": view_fields(field_names),
            "rowLimit": str(row_limit),
            "queryOptions": query_options(),
        }
        if change_token:
            params["changeToken"] = change_token
        params["contains"] = None
        result = self.client.call("GetListItemChangesSinceToken", params)

        page = self._parse_items(result)
        batch = ChangeBatch(rows=page.rows)

        changes = find_one(result, "Changes")
        if changes is not None:
            batch.last_change_token = changes.get("LastChangeToken")
            batch.more_changes = (changes.get("MoreChanges") or "").upper() == "TRUE"
            for id_el in find_all(changes, "Id"):
                change_type = (id_el.get("ChangeType") or "").lower()
                text = text_content(id_el).strip()
                if change_type == "invalidtoken" or change_type == "invalid":
                    batch.invalid_token = True
                    continue
                if change_type == "delete":
                    try:
                        batch.deleted_ids.append(int(text))
                    except ValueError:
                        log.warning("sync.undecodable_delete_id", raw=text)
        return batch

    # ---- 6.6 attachments ----

    def get_attachment_collection(self, list_name: str, item_id: int | str) -> list[str]:
        """Fallback only — prefer ``ows_AttachmentUrls`` from the item itself."""
        result = self.client.call(
            "GetAttachmentCollection",
            {"listName": list_name, "listItemID": str(item_id)},
        )
        return [text_content(el).strip() for el in find_all(result, "Attachment") if text_content(el).strip()]


def is_system_list(list_info: ListInfo) -> bool:
    return list_info.title in SYSTEM_LIST_TITLES
