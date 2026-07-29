"""Table-driven tests for every row of the decoding table, plus the traps.

This is the highest-value file in the suite: a decoder bug here corrupts twenty
years of service history silently, and the server cannot be re-crawled cheaply.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from spconnect.decode import (
    DecodeContext,
    RowDecoder,
    coerce_item_id,
    decode_attachment_urls,
    decode_attachments,
    decode_boolean,
    decode_calculated,
    decode_choice,
    decode_datetime,
    decode_fsobjtype,
    decode_int,
    decode_lookup,
    decode_lookup_multi,
    decode_multichoice,
    decode_number,
    decode_url,
    decode_user,
    decode_user_multi,
    decode_value,
    json_default,
    split_multi,
    strip_ows,
)
from spconnect.models import FieldDef


class Sink:
    """Collects warnings so tests can assert on them instead of on log output."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event: str, payload: dict) -> None:
        self.events.append((event, payload))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]


@pytest.fixture
def sink() -> Sink:
    return Sink()


@pytest.fixture
def ctx(sink: Sink) -> DecodeContext:
    return DecodeContext(list_title="Servicefälle", list_guid="{1111}", on_warning=sink)


# --------------------------------------------------------------------------- #
# split_multi
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (";#Reparatur;#Garantie;#", ["Reparatur", "Garantie"]),
        ("42;#Müller", ["42", "Müller"]),
        (";#;#", []),
        (";#", []),
        ("", []),
        ("   ", []),
        ("einzelwert", ["einzelwert"]),
        ("42;#Müller;#57;#Beta AG", ["42", "Müller", "57", "Beta AG"]),
    ],
)
def test_split_multi_strips_wrapping_delimiters(raw: str, expected: list[str]) -> None:
    assert split_multi(raw) == expected


# --------------------------------------------------------------------------- #
# the decoding table, §7
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("field_type", "raw", "expected"),
    [
        ("Text", "Getriebeschaden", "Getriebeschaden"),
        ("Text", "", ""),
        ("Note", "<div>Geräusch &amp; Vibration</div>", "<div>Geräusch &amp; Vibration</div>"),
        ("Number", "1234.500000000000", 1234.5),
        ("Currency", "0.000000000000", 0.0),
        ("Counter", "42", 42),
        ("Integer", "-7", -7),
        ("Boolean", "1", True),
        ("Boolean", "0", False),
        ("Choice", "Hoch", "Hoch"),
        ("MultiChoice", ";#Reparatur;#Garantie;#", ["Reparatur", "Garantie"]),
        ("MultiChoice", "", []),
        ("Lookup", "42;#Müller Maschinenbau GmbH", {"id": 42, "value": "Müller Maschinenbau GmbH"}),
        (
            "LookupMulti",
            "42;#Müller;#57;#Beta AG",
            [{"id": 42, "value": "Müller"}, {"id": 57, "value": "Beta AG"}],
        ),
        ("User", "12;#CONTOSO\\jdoe", {"id": 12, "value": "CONTOSO\\jdoe"}),
        (
            "UserMulti",
            "12;#CONTOSO\\jdoe;#15;#CONTOSO\\mmueller",
            [{"id": 12, "value": "CONTOSO\\jdoe"}, {"id": 15, "value": "CONTOSO\\mmueller"}],
        ),
        (
            "URL",
            "http://example.com, Anzeigetext",
            {"url": "http://example.com", "description": "Anzeigetext"},
        ),
        ("Attachments", "1", True),
        ("Attachments", "0", False),
    ],
)
def test_decode_table(field_type: str, raw: str, expected: object) -> None:
    assert decode_value(field_type, raw) == expected


def test_decode_datetime_is_timezone_aware_utc() -> None:
    value = decode_value("DateTime", "2019-04-03T14:22:11Z")
    assert value == datetime(2019, 4, 3, 14, 22, 11, tzinfo=UTC)
    assert value.tzinfo is not None


@pytest.mark.parametrize(
    "raw",
    ["2019-04-03T14:22:11Z", "2019-04-03T14:22:11", "2019-04-03 14:22:11", "2019-04-03 14:22:11.000"],
)
def test_decode_datetime_accepts_the_forms_this_farm_emits(raw: str) -> None:
    value = decode_datetime(raw)
    assert value is not None
    assert value.utcoffset() == UTC.utcoffset(None)
    assert (value.year, value.month, value.day, value.hour) == (2019, 4, 3, 14)


def test_decode_datetime_date_only() -> None:
    assert decode_datetime("2019-04-03") == datetime(2019, 4, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("float;#1234.5", 1234.5),
        ("int;#42", 42),
        ("boolean;#1", True),
        ("string;#Süd", "Süd"),
        ("datetime;#2019-04-03 14:22:11", datetime(2019, 4, 3, 14, 22, 11, tzinfo=UTC)),
    ],
)
def test_decode_calculated_strips_the_type_prefix(raw: str, expected: object) -> None:
    assert decode_calculated(raw) == expected


def test_decode_calculated_without_prefix_uses_result_type() -> None:
    assert decode_calculated("1234.5", result_type="Number") == 1234.5
    assert decode_calculated("weder noch") == "weder noch"


def test_decode_calculated_unknown_prefix_warns_and_keeps_the_value(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_calculated("currencyX;#12", ctx) == "12"
    assert "decode.calculated_unknown_prefix" in sink.names


# --------------------------------------------------------------------------- #
# empty / None / malformed
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field_type",
    ["Text", "Number", "Counter", "Boolean", "DateTime", "MultiChoice", "Lookup", "LookupMulti", "URL"],
)
def test_none_decodes_to_none_for_every_type(field_type: str) -> None:
    assert decode_value(field_type, None) is None


@pytest.mark.parametrize(
    ("field_type", "expected"),
    [
        ("Number", None),
        ("Counter", None),
        ("Boolean", None),
        ("DateTime", None),
        ("MultiChoice", []),
        ("LookupMulti", []),
        ("Lookup", None),
        ("URL", None),
    ],
)
def test_empty_string_decodes_to_the_empty_value(field_type: str, expected: object) -> None:
    assert decode_value(field_type, "") == expected


def test_unparseable_number_warns_rather_than_raising(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_number("1.234,50", ctx) is None
    assert sink.names == ["decode.number_unparseable"]


def test_unparseable_int_falls_back_through_float(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_int("42.0000000000", ctx) == 42
    assert decode_int("keine Zahl", ctx) is None
    assert "decode.int_unparseable" in sink.names


def test_unparseable_boolean_warns(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_boolean("vielleicht", ctx) is None
    assert sink.names == ["decode.boolean_unparseable"]


def test_unparseable_datetime_warns(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_datetime("14.03.2009", ctx) is None
    assert sink.names == ["decode.datetime_unparseable"]


def test_lookup_with_odd_token_count_warns_and_keeps_the_id(ctx: DecodeContext, sink: Sink) -> None:
    # A display value that itself contains ";#" — theoretically possible, and
    # silently corrupting if unhandled.
    result = decode_lookup("42;#Firma A;#B GmbH", ctx)
    assert result == {"id": 42, "value": "Firma A;#B GmbH"}
    assert sink.names == ["decode.lookup_odd_tokens"]
    payload = sink.events[0][1]
    assert payload["list"] == "Servicefälle"
    assert payload["tokens"] == 3


def test_lookup_multi_with_odd_token_count_warns_and_drops_the_orphan(ctx: DecodeContext, sink: Sink) -> None:
    result = decode_lookup_multi("42;#Müller;#57", ctx)
    assert result == [{"id": 42, "value": "Müller"}]
    assert sink.names == ["decode.lookup_multi_odd_tokens"]


def test_lookup_with_bare_id_only() -> None:
    assert decode_lookup("42") == {"id": 42, "value": None}


def test_lookup_without_id_warns(ctx: DecodeContext, sink: Sink) -> None:
    assert decode_lookup("Müller GmbH", ctx) == {"id": None, "value": "Müller GmbH"}
    assert sink.names == ["decode.lookup_without_id"]


def test_lookup_with_non_numeric_id_keeps_the_whole_string() -> None:
    assert decode_lookup("abc;#Müller") == {"id": None, "value": "abc;#Müller"}


def test_lookup_keeps_the_id_when_the_display_value_is_empty() -> None:
    assert decode_lookup("42;#") == {"id": 42, "value": None}


def test_warnings_carry_list_item_and_field(sink: Sink) -> None:
    ctx = DecodeContext(list_title="Servicefälle", list_guid="{1111}", on_warning=sink)
    ctx.item_id = 4711
    ctx.field_name = "Kunde"
    decode_lookup("42;#A;#B", ctx)
    _, payload = sink.events[0]
    assert (payload["list"], payload["item_id"], payload["field"]) == ("Servicefälle", 4711, "Kunde")


# --------------------------------------------------------------------------- #
# individual encodings
# --------------------------------------------------------------------------- #


def test_multichoice_with_a_choice_containing_an_ampersand() -> None:
    assert decode_multichoice(";#Kulanz &amp; Sonstiges;#Garantie;#") == [
        "Kulanz &amp; Sonstiges",
        "Garantie",
    ]


def test_choice_tolerates_the_multi_wrapper() -> None:
    assert decode_choice(";#Hoch;#") == "Hoch"
    assert decode_choice("Hoch") == "Hoch"


def test_url_without_a_description() -> None:
    assert decode_url("http://intranet/sop/17") == {
        "url": "http://intranet/sop/17",
        "description": None,
    }


def test_url_description_may_contain_commas() -> None:
    assert decode_url("http://x/y, Süd, Halle 3") == {"url": "http://x/y", "description": "Süd, Halle 3"}


def test_user_and_user_multi_share_the_lookup_encoding() -> None:
    assert decode_user("12;#CONTOSO\\jdoe") == {"id": 12, "value": "CONTOSO\\jdoe"}
    assert decode_user_multi("") == []


@pytest.mark.parametrize(("raw", "expected"), [("0", 0), ("1", 1), ("1;#0", 0), ("1;#1", 1), ("", None)])
def test_fsobjtype_handles_bare_and_prefixed_forms(raw: str, expected: int | None) -> None:
    assert decode_fsobjtype(raw) == expected


def test_attachment_urls_split_on_the_delimiter() -> None:
    raw = "http://sp/a/1/foto.jpg;#http://sp/a/1/messwerte.csv"
    assert decode_attachment_urls(raw) == ["http://sp/a/1/foto.jpg", "http://sp/a/1/messwerte.csv"]
    assert decode_attachment_urls("") == []
    assert decode_attachment_urls(None) == []


def test_attachments_flag_on_a_document_library_url() -> None:
    assert decode_attachments("http://sp/x.pdf") is True
    assert decode_attachments(None) is None


def test_file_columns_are_lookup_encoded_on_the_wire() -> None:
    # ows_FileLeafRef is typed "File" in the schema but arrives as "1;#name.pdf".
    assert decode_value("File", "1;#Handbuch Straße.pdf") == {
        "id": 1,
        "value": "Handbuch Straße.pdf",
    }


def test_unknown_field_type_falls_back_to_the_raw_string() -> None:
    assert decode_value("SomeFutureType", "roher Wert") == "roher Wert"


# --------------------------------------------------------------------------- #
# whole rows
# --------------------------------------------------------------------------- #


def _cases_fields() -> dict[str, FieldDef]:
    return {
        "Title": FieldDef(name="Title", type="Text", display_name="Titel"),
        "Kunde": FieldDef(name="Kunde", type="Lookup", display_name="Kunde"),
        "Kategorie": FieldDef(name="Kategorie", type="MultiChoice", display_name="Kategorie", mult=True),
        "Kosten": FieldDef(name="Kosten", type="Currency", display_name="Kosten"),
        "Eingegangen": FieldDef(name="Eingegangen", type="DateTime", display_name="Eingegangen"),
        "Case_x0020_Number": FieldDef(name="Case_x0020_Number", type="Text", display_name="Case Number"),
        "Kostensch_x00e4_tzung": FieldDef(
            name="Kostensch_x00e4_tzung", type="Calculated", result_type="Number"
        ),
    }


def test_decode_row_keys_on_internal_names_and_skips_metainfo() -> None:
    decoder = RowDecoder(_cases_fields(), list_title="Servicefälle")
    raw = {
        "ows_ID": "4711",
        "ows_Title": "Getriebeschaden",
        "ows_Case_x0020_Number": "SF-2009-0001",
        "ows_Kunde": "42;#Müller Maschinenbau GmbH",
        "ows_Kategorie": ";#Reparatur;#Garantie;#",
        "ows_Kosten": "1234.500000000000",
        "ows_Eingegangen": "2009-03-14T08:11:00Z",
        "ows_Kostensch_x00e4_tzung": "float;#1469.055",
        "ows_MetaInfo": "vti_parserversion:SR|12.0.0.6421",
        "ows_FSObjType": "1;#0",
        "ows_AttachmentUrls": "http://sp/a/4711/foto.jpg",
        "not_an_ows_key": "ignored",
    }
    decoded = decoder.decode_row(raw)

    assert "MetaInfo" not in decoded
    assert "not_an_ows_key" not in decoded
    assert decoded["ID"] == 4711
    assert decoded["Case_x0020_Number"] == "SF-2009-0001"
    assert decoded["Kunde"] == {"id": 42, "value": "Müller Maschinenbau GmbH"}
    assert decoded["Kategorie"] == ["Reparatur", "Garantie"]
    assert decoded["Kosten"] == 1234.5
    assert decoded["Eingegangen"] == datetime(2009, 3, 14, 8, 11, tzinfo=UTC)
    assert decoded["Kostensch_x00e4_tzung"] == 1469.055
    assert decoded["FSObjType"] == 0
    assert decoded["AttachmentUrls"] == ["http://sp/a/4711/foto.jpg"]


def test_decode_row_uses_system_types_for_columns_missing_from_the_schema() -> None:
    decoder = RowDecoder({})
    decoded = decoder.decode_row(
        {"ows_ID": "9", "ows_Created": "2009-03-14T08:11:00Z", "ows_Author": "1;#CONTOSO\\admin"}
    )
    assert decoded["ID"] == 9
    assert decoded["Created"] == datetime(2009, 3, 14, 8, 11, tzinfo=UTC)
    assert decoded["Author"] == {"id": 1, "value": "CONTOSO\\admin"}


def test_decode_row_counts_warnings_for_the_final_summary() -> None:
    decoder = RowDecoder({"Kunde": FieldDef(name="Kunde", type="Lookup")}, list_title="Servicefälle")
    decoder.decode_row({"ows_ID": "1", "ows_Kunde": "42;#A;#B"})
    assert decoder.warning_count == 1


def test_strip_ows_drops_the_prefix_and_metainfo() -> None:
    assert strip_ows({"ows_ID": "1", "ows_MetaInfo": "x", "other": "y"}) == {"ID": "1"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [({"ows_ID": "42"}, 42), ({"ows_ID": "42;#42"}, 42), ({}, None), ({"ows_ID": "x"}, None)],
)
def test_coerce_item_id(raw: dict[str, str], expected: int | None) -> None:
    assert coerce_item_id(raw) == expected


def test_json_default_renders_datetimes_as_utc_zulu() -> None:
    assert json_default(datetime(2009, 3, 14, 8, 11, tzinfo=UTC)) == "2009-03-14T08:11:00Z"
    assert json_default(datetime(2009, 3, 14, 8, 11)) == "2009-03-14T08:11:00Z"
    with pytest.raises(TypeError):
        json_default(object())
