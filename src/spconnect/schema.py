"""Field schema parsing and the lookup graph.

The lookup graph is the recovered data model of a CRM nobody documented. It is
the single most valuable artifact of the crawl: the downstream pipeline reads
it to decide which lists to denormalise into which documents.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from lxml import etree

from .config import get_logger
from .models import FieldDef, GraphNode, ListInfo, ListSchema, LookupEdge, LookupGraph, normalise_guid
from .soap import find_all, find_one, text_content

log = get_logger(__name__)

_ESCAPE_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")

_TRUE = frozenset({"true", "1", "yes"})


def _as_bool(value: str | None) -> bool:
    return value is not None and value.strip().lower() in _TRUE


def _as_int(value: str | None, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value.strip())
    except ValueError:
        return default


# --------------------------------------------------------------------------- #
# Internal name escaping
# --------------------------------------------------------------------------- #


def unescape_internal_name(name: str) -> str:
    """``Case_x0020_Number`` -> ``Case Number``."""
    if not name:
        return name

    def repl(match: re.Match[str]) -> str:
        return chr(int(match.group(1), 16))

    return _ESCAPE_RE.sub(repl, name)


def escape_internal_name(name: str) -> str:
    """``Case Number`` -> ``Case_x0020_Number``.

    Anything outside ASCII ``[A-Za-z0-9]`` is escaped, which covers the umlauts
    this farm is full of (``ü`` -> ``_x00fc_``).
    """
    out: list[str] = []
    for char in name:
        if char.isascii() and char.isalnum():
            out.append(char)
        else:
            out.append(f"_x{ord(char):04x}_")
    return "".join(out)


# --------------------------------------------------------------------------- #
# Field parsing
# --------------------------------------------------------------------------- #


def parse_field(el: etree._Element) -> FieldDef:
    """Parse one ``<Field>`` element from ``Lists.GetList``."""
    attrs = {str(k): str(v) for k, v in el.attrib.items()}
    name = attrs.get("Name") or attrs.get("StaticName") or attrs.get("DisplayName") or ""

    choices = [text_content(choice).strip() for choice in find_all(el, "CHOICE")]
    formula_el = find_one(el, "Formula")
    formula = text_content(formula_el) if formula_el is not None else None

    lookup_list = attrs.get("List")
    if lookup_list and lookup_list != "Self":
        lookup_list = normalise_guid(lookup_list)

    return FieldDef(
        id=normalise_guid(attrs.get("ID")) if attrs.get("ID") else "",
        name=name,
        static_name=attrs.get("StaticName", name),
        display_name=attrs.get("DisplayName", name),
        type=attrs.get("Type", "Text"),
        required=_as_bool(attrs.get("Required")),
        hidden=_as_bool(attrs.get("Hidden")),
        read_only=_as_bool(attrs.get("ReadOnly")),
        lookup_list=lookup_list,
        show_field=attrs.get("ShowField"),
        mult=_as_bool(attrs.get("Mult")),
        col_name=attrs.get("ColName"),
        format=attrs.get("Format"),
        result_type=attrs.get("ResultType"),
        max_length=_as_int(attrs.get("MaxLength"), 0) or None,
        choices=choices,
        formula=formula,
        raw_attributes=attrs,
    )


def parse_fields(list_element: etree._Element) -> list[FieldDef]:
    """Parse the ``<Fields>`` child of a ``GetList`` response."""
    fields_el = find_one(list_element, "Fields")
    source = fields_el if fields_el is not None else list_element
    return [parse_field(el) for el in find_all(source, "Field")]


def parse_list_attributes(el: etree._Element, web_url: str = "") -> ListInfo:
    """Parse a ``<List>`` element from ``GetListCollection`` or ``GetList``."""
    attrs = {str(k): str(v) for k, v in el.attrib.items()}
    return ListInfo(
        guid=normalise_guid(attrs.get("ID") or attrs.get("Guid") or ""),
        title=attrs.get("Title", ""),
        description=attrs.get("Description", ""),
        base_type=attrs.get("BaseType", "0"),
        server_template=attrs.get("ServerTemplate", ""),
        item_count=_as_int(attrs.get("ItemCount")),
        hidden=_as_bool(attrs.get("Hidden")),
        root_folder=attrs.get("RootFolder", ""),
        default_view_url=attrs.get("DefaultViewUrl", ""),
        enable_attachments=_as_bool(attrs.get("EnableAttachments")),
        has_unique_scopes=_as_bool(attrs.get("HasUniqueScopes")),
        created=attrs.get("Created"),
        modified=attrs.get("Modified"),
        web_url=web_url,
        raw_attributes=attrs,
    )


# --------------------------------------------------------------------------- #
# Lookup graph
# --------------------------------------------------------------------------- #


def build_lookup_graph(schemas: Iterable[ListSchema]) -> LookupGraph:
    """One node per list, one edge per Lookup/LookupMulti column.

    ``List="Self"`` resolves to the containing list. A ``List`` GUID matching no
    crawled list is kept as a *dangling* edge rather than dropped — knowing that
    a foreign key points somewhere out of scope is information.
    """
    schema_list = list(schemas)
    by_guid = {s.list_info.guid: s.list_info for s in schema_list}

    nodes = [
        GraphNode(
            list_guid=s.list_info.guid,
            title=s.list_info.title,
            web_url=s.list_info.web_url,
            item_count=s.list_info.item_count,
            base_type=s.list_info.base_type,
            base_type_name=s.list_info.base_type_name,
        )
        for s in schema_list
    ]

    edges: list[LookupEdge] = []
    for schema in schema_list:
        source = schema.list_info
        for field in schema.fields:
            if not field.is_lookup or not field.lookup_list:
                continue
            self_ref = field.lookup_list == "Self"
            target_guid = source.guid if self_ref else normalise_guid(field.lookup_list)
            target = by_guid.get(target_guid)
            edges.append(
                LookupEdge(
                    source_list_guid=source.guid,
                    source_list_title=source.title,
                    source_web_url=source.web_url,
                    target_list_guid=target_guid,
                    target_list_title=target.title if target else None,
                    field_name=field.name,
                    field_display_name=field.display_name or field.name,
                    show_field=field.show_field,
                    multi=field.mult or field.type == "LookupMulti",
                    self_reference=self_ref,
                    dangling=target is None,
                )
            )

    dangling = sum(1 for e in edges if e.dangling)
    log.info("graph.built", lists=len(nodes), edges=len(edges), dangling_edges=dangling)
    return LookupGraph(nodes=nodes, edges=edges)


def _mermaid_id(guid: str) -> str:
    return "L" + re.sub(r"[^0-9a-zA-Z]", "", guid)[:32]


def _escape_label(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ").strip() or "(untitled)"


def render_mermaid(graph: LookupGraph) -> str:
    """``graph LR`` rendering — paste into any Mermaid viewer."""
    lines = ["graph LR"]
    for node in graph.nodes:
        shape_open, shape_close = ("[(", ")]") if node.base_type == "1" else ("[", "]")
        label = f"{_escape_label(node.title)}<br/>{node.item_count} items"
        lines.append(f'    {_mermaid_id(node.list_guid)}{shape_open}"{label}"{shape_close}')

    known = {n.list_guid for n in graph.nodes}
    dangling_targets = {e.target_list_guid for e in graph.edges if e.target_list_guid not in known}
    for guid in sorted(dangling_targets):
        lines.append(f'    {_mermaid_id(guid)}["out of scope<br/>{_escape_label(guid)}"]')

    for edge in graph.edges:
        label = _escape_label(edge.field_display_name)
        if edge.multi:
            label += " (n)"
        arrow = "-.->" if edge.dangling else "-->"
        lines.append(
            f'    {_mermaid_id(edge.source_list_guid)} {arrow}|"{label}"| '
            f"{_mermaid_id(edge.target_list_guid)}"
        )

    for guid in sorted(dangling_targets):
        lines.append(f"    style {_mermaid_id(guid)} stroke-dasharray: 5 5")
    return "\n".join(lines) + "\n"


def render_dot(graph: LookupGraph) -> str:
    """Graphviz rendering, for people who prefer ``dot -Tsvg``."""
    lines = ["digraph lookups {", "    rankdir=LR;", '    node [shape=box, fontname="Helvetica"];']
    for node in graph.nodes:
        shape = "cylinder" if node.base_type == "1" else "box"
        label = f"{_escape_label(node.title)}\\n{node.item_count} items"
        lines.append(f'    "{node.list_guid}" [label="{label}", shape={shape}];')

    known = {n.list_guid for n in graph.nodes}
    for guid in sorted({e.target_list_guid for e in graph.edges if e.target_list_guid not in known}):
        lines.append(f'    "{guid}" [label="out of scope\\n{guid}", style=dashed];')

    for edge in graph.edges:
        label = _escape_label(edge.field_display_name) + (" (n)" if edge.multi else "")
        style = ", style=dashed" if edge.dangling else ""
        lines.append(f'    "{edge.source_list_guid}" -> "{edge.target_list_guid}" [label="{label}"{style}];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def graph_summary(graph: LookupGraph) -> dict[str, int]:
    return {
        "lists": len(graph.nodes),
        "edges": len(graph.edges),
        "dangling_edges": len(graph.dangling_edges),
        "self_references": sum(1 for e in graph.edges if e.self_reference),
        "multi_value_edges": sum(1 for e in graph.edges if e.multi),
    }


def viewfields_names(fields: Sequence[FieldDef]) -> list[str]:
    """Which columns to request explicitly in ``GetListItems``.

    With an empty ``viewName`` *and* an empty ``viewFields`` SharePoint returns
    the default view's columns, not all of them — a quiet way to lose data. So
    the crawler always asks for every column by name, minus the property bag.
    """
    seen: set[str] = set()
    names: list[str] = []
    for field in fields:
        if not field.name or field.name in seen or field.name == "MetaInfo":
            continue
        seen.add(field.name)
        names.append(field.name)
    for system in (
        "ID",
        "UniqueId",
        "GUID",
        "Created",
        "Modified",
        "Author",
        "Editor",
        "ContentType",
        "FSObjType",
        "FileRef",
        "FileLeafRef",
        "EncodedAbsUrl",
        "Attachments",
    ):
        if system not in seen:
            seen.add(system)
            names.append(system)
    return names
