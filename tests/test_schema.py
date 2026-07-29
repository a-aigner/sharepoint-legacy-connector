"""Field parsing, internal-name escaping, and the lookup graph."""

from __future__ import annotations

import pytest
from lxml import etree

from conftest import CASES, DANGLING, DOKUMENTE, KUNDEN, WEB1, fixture_bytes
from spconnect.models import ListInfo, ListSchema
from spconnect.schema import (
    build_lookup_graph,
    escape_internal_name,
    graph_summary,
    parse_field,
    parse_fields,
    parse_list_attributes,
    render_dot,
    render_mermaid,
    unescape_internal_name,
    viewfields_names,
)
from spconnect.soap import find_one, parse_response


def _list_element(name: str) -> etree._Element:
    result = parse_response(fixture_bytes(name), "GetList")
    element = find_one(result, "List")
    assert element is not None
    return element


@pytest.fixture
def cases_schema() -> ListSchema:
    element = _list_element("lists_getlist_cases.xml")
    return ListSchema(list_info=parse_list_attributes(element, WEB1), fields=parse_fields(element))


@pytest.fixture
def kunden_schema() -> ListSchema:
    element = _list_element("lists_getlist_kunden.xml")
    return ListSchema(list_info=parse_list_attributes(element, WEB1), fields=parse_fields(element))


@pytest.fixture
def dokumente_schema() -> ListSchema:
    element = _list_element("lists_getlist_dokumente.xml")
    return ListSchema(list_info=parse_list_attributes(element, WEB1), fields=parse_fields(element))


# --------------------------------------------------------------------------- #
# internal name escaping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("escaped", "plain"),
    [
        ("Case_x0020_Number", "Case Number"),
        ("Kostensch_x00e4_tzung", "Kostenschätzung"),
        ("Title", "Title"),
        ("Gr_x00f6__x00df_e", "Größe"),
        ("", ""),
    ],
)
def test_unescape_internal_name(escaped: str, plain: str) -> None:
    assert unescape_internal_name(escaped) == plain


@pytest.mark.parametrize(
    "display", ["Case Number", "Kostenschätzung", "Größe", "Titel", "Preis (netto)", "A/B", "Straße 1"]
)
def test_escape_unescape_round_trip(display: str) -> None:
    assert unescape_internal_name(escape_internal_name(display)) == display


def test_escape_uses_the_sharepoint_form() -> None:
    assert escape_internal_name("Case Number") == "Case_x0020_Number"
    assert escape_internal_name("Kostenschätzung") == "Kostensch_x00e4_tzung"


def test_escaped_underscore_round_trips() -> None:
    # A literal underscore has to be escaped too, or "_x0020_" in a display name
    # would round-trip into a space.
    assert unescape_internal_name(escape_internal_name("Case_x0020_Number")) == "Case_x0020_Number"


# --------------------------------------------------------------------------- #
# field parsing
# --------------------------------------------------------------------------- #


def test_parse_fields_captures_every_documented_attribute(cases_schema: ListSchema) -> None:
    fields = cases_schema.field_map()

    title = fields["Title"]
    assert (title.display_name, title.type, title.required, title.col_name) == (
        "Titel",
        "Text",
        True,
        "nvarchar1",
    )

    number = fields["Case_x0020_Number"]
    assert number.display_name == "Case Number"
    assert number.unescaped_name == "Case Number"

    kunde = fields["Kunde"]
    assert kunde.type == "Lookup"
    assert kunde.lookup_list == KUNDEN
    assert kunde.show_field == "Title"
    assert kunde.is_lookup

    anlagen = fields["Betroffene_x0020_Anlagen"]
    assert anlagen.type == "LookupMulti"
    assert anlagen.mult is True
    assert anlagen.lookup_list == DANGLING

    eingegangen = fields["Eingegangen"]
    assert eingegangen.format == "DateTime"
    assert fields["Faellig"].format == "DateOnly"

    assert fields["FSObjType"].hidden is True
    assert fields["ID"].read_only is True


def test_parse_fields_captures_choices_and_formulas(cases_schema: ListSchema) -> None:
    fields = cases_schema.field_map()
    assert fields["Kategorie"].choices == ["Reparatur", "Garantie", "Wartung", "Kulanz & Sonstiges"]
    assert fields["Prioritaet"].choices == ["Hoch", "Mittel", "Niedrig"]
    assert fields["Kostensch_x00e4_tzung"].formula == "=Kosten*1.19"
    assert fields["Kostensch_x00e4_tzung"].result_type == "Number"
    assert fields["Wiedervorlage"].result_type == "DateTime"


def test_parse_list_attributes(cases_schema: ListSchema) -> None:
    info = cases_schema.list_info
    assert info.guid == CASES
    assert info.title == "Servicefälle"
    assert info.item_count == 3
    assert info.base_type == "0"
    assert info.base_type_name == "generic_list"
    assert info.enable_attachments is True
    assert info.has_unique_scopes is True
    assert info.web_url == WEB1


def test_document_library_base_type(dokumente_schema: ListSchema) -> None:
    assert dokumente_schema.list_info.is_document_library
    assert dokumente_schema.list_info.base_type_name == "document_library"


def test_parse_field_defaults_when_attributes_are_missing() -> None:
    element = etree.fromstring('<Field Name="Nackt"/>')
    field = parse_field(element)
    assert (field.name, field.type, field.required, field.choices) == ("Nackt", "Text", False, [])


def test_display_names_map_internal_to_german_labels(cases_schema: ListSchema) -> None:
    names = cases_schema.display_names()
    assert names["Case_x0020_Number"] == "Case Number"
    assert names["Eingegangen"] == "Eingegangen am"
    assert names["Vorgaenger"] == "Vorgänger"


# --------------------------------------------------------------------------- #
# lookup graph
# --------------------------------------------------------------------------- #


@pytest.fixture
def graph(cases_schema: ListSchema, kunden_schema: ListSchema, dokumente_schema: ListSchema):
    return build_lookup_graph([cases_schema, kunden_schema, dokumente_schema])


def test_graph_has_one_node_per_list(graph) -> None:
    assert {n.list_guid for n in graph.nodes} == {CASES, KUNDEN, DOKUMENTE}
    cases_node = next(n for n in graph.nodes if n.list_guid == CASES)
    assert (cases_node.title, cases_node.item_count, cases_node.web_url) == ("Servicefälle", 3, WEB1)


def test_graph_edge_carries_the_field_metadata(graph) -> None:
    edge = next(e for e in graph.edges if e.field_name == "Kunde")
    assert edge.source_list_guid == CASES
    assert edge.target_list_guid == KUNDEN
    assert edge.target_list_title == "Kunden"
    assert edge.show_field == "Title"
    assert edge.multi is False
    assert edge.dangling is False


def test_multi_value_lookup_is_flagged(graph) -> None:
    edge = next(e for e in graph.edges if e.field_name == "Betroffene_x0020_Anlagen")
    assert edge.multi is True


def test_self_lookup_resolves_to_the_containing_list(graph) -> None:
    edge = next(e for e in graph.edges if e.field_name == "Vorgaenger")
    assert edge.self_reference is True
    assert edge.target_list_guid == CASES
    assert edge.dangling is False


def test_dangling_edge_is_kept_not_dropped(graph) -> None:
    edge = next(e for e in graph.edges if e.target_list_guid == DANGLING)
    assert edge.dangling is True
    assert edge.target_list_title is None
    assert graph.dangling_edges == [edge]


def test_user_lookups_are_not_graph_edges(graph) -> None:
    # List="UserInfo" is not a crawled list; it must not appear as an edge to a
    # bogus GUID, and it must not be reported as dangling either.
    assert not any(e.field_name in ("Bearbeiter", "Techniker", "Author", "Editor") for e in graph.edges)


def test_incoming_edge_from_the_document_library(graph) -> None:
    edge = next(e for e in graph.edges if e.field_name == "Anlage")
    assert (edge.source_list_guid, edge.target_list_guid) == (DOKUMENTE, CASES)


def test_graph_summary(graph) -> None:
    summary = graph_summary(graph)
    assert summary["lists"] == 3
    assert summary["dangling_edges"] == 1
    assert summary["self_references"] == 1
    assert summary["multi_value_edges"] == 1


def test_render_mermaid_is_renderable(graph) -> None:
    rendered = render_mermaid(graph)
    assert rendered.startswith("graph LR")
    assert "Servicefälle" in rendered
    assert "-->|" in rendered
    assert "-.->|" in rendered  # the dangling edge is dashed
    assert "out of scope" in rendered
    # Node ids must be Mermaid-safe: no braces or dashes.
    for line in rendered.splitlines()[1:]:
        assert "{" not in line.split('"')[0]


def test_render_mermaid_escapes_quotes_in_titles() -> None:
    info = ListInfo(guid=CASES, title='Fälle "Süd"', web_url=WEB1)
    rendered = render_mermaid(build_lookup_graph([ListSchema(list_info=info, fields=[])]))
    assert "Fälle 'Süd'" in rendered


def test_render_dot(graph) -> None:
    rendered = render_dot(graph)
    assert rendered.startswith("digraph lookups {")
    assert rendered.rstrip().endswith("}")
    assert "style=dashed" in rendered


def test_graph_of_nothing_is_empty() -> None:
    graph = build_lookup_graph([])
    assert graph.nodes == [] and graph.edges == []
    assert render_mermaid(graph).strip() == "graph LR"


# --------------------------------------------------------------------------- #
# viewfields
# --------------------------------------------------------------------------- #


def test_viewfields_requests_every_column_except_the_property_bag(cases_schema: ListSchema) -> None:
    names = viewfields_names(cases_schema.fields)
    assert "MetaInfo" not in names
    assert "Case_x0020_Number" in names
    assert "Kunde" in names
    assert len(names) == len(set(names))


def test_viewfields_adds_the_system_columns_when_the_schema_omits_them() -> None:
    names = viewfields_names([])
    assert {"ID", "Created", "Modified", "Author", "Editor", "Attachments"} <= set(names)
