"""Decoding of SharePoint ``ows_*`` wire values.

``GetListItems`` hands back every value as a string attribute on ``<z:row>``.
The encodings are undocumented in any one place and are where this project
either succeeds or quietly corrupts twenty years of service history. Every
decoder here is total: malformed input produces a warning and a best-effort
value, never an exception, because one bad row must not abort a list.

Rows are always persisted in *both* forms — raw and decoded. Storage is cheap;
re-crawling this server is not.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from .config import get_logger
from .models import FieldDef

log = get_logger(__name__)

DELIM = ";#"
OWS_PREFIX = "ows_"

#: Property bag with its own internal format. Explicitly out of scope.
SKIPPED_FIELDS = frozenset({"MetaInfo", "ows_MetaInfo"})

#: System columns worth keeping even when they are absent from the list schema.
SYSTEM_FIELD_TYPES: dict[str, str] = {
    "ID": "Counter",
    "UniqueId": "Text",
    "GUID": "Text",
    "Created": "DateTime",
    "Modified": "DateTime",
    "Created_x0020_Date": "DateTime",
    "Last_x0020_Modified": "DateTime",
    "Author": "User",
    "Editor": "User",
    "ContentType": "Text",
    "ContentTypeId": "Text",
    "FSObjType": "Integer",
    "FileRef": "Lookup",
    "FileLeafRef": "Lookup",
    "FileDirRef": "Lookup",
    "EncodedAbsUrl": "Text",
    "AttachmentUrls": "Text",
    "Attachments": "Attachments",
    "owshiddenversion": "Integer",
    "_ModerationStatus": "Integer",
    "_Level": "Integer",
    "Order": "Number",
    "PermMask": "Text",
    "ServerUrl": "Text",
    "BaseName": "Text",
    "FileSizeDisplay": "Integer",
}

TRUE_TOKENS = frozenset({"1", "true", "yes", "on", "-1"})
FALSE_TOKENS = frozenset({"0", "false", "no", "off", ""})

_DATE_PATTERNS = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d",
)

WarnFn = Callable[[str, dict[str, Any]], None]


class DecodeContext:
    """Carries who/where a value came from so warnings are actionable."""

    def __init__(
        self,
        list_title: str = "",
        list_guid: str = "",
        web_url: str = "",
        on_warning: WarnFn | None = None,
    ) -> None:
        self.list_title = list_title
        self.list_guid = list_guid
        self.web_url = web_url
        self.on_warning = on_warning
        self.item_id: str | int | None = None
        self.field_name: str = ""
        self.warning_count = 0

    def warn(self, event: str, **kwargs: Any) -> None:
        self.warning_count += 1
        payload: dict[str, Any] = {
            "list": self.list_title,
            "list_guid": self.list_guid,
            "web_url": self.web_url,
            "item_id": self.item_id,
            "field": self.field_name,
            **kwargs,
        }
        log.warning(event, **payload)
        if self.on_warning is not None:
            self.on_warning(event, payload)


_NULL_CONTEXT = DecodeContext()


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def split_multi(value: str) -> list[str]:
    """Split a ``;#``-delimited value, dropping the wrapping delimiters.

    ``";#Reparatur;#Garantie;#"`` -> ``["Reparatur", "Garantie"]``.
    ``"42;#Müller"`` -> ``["42", "Müller"]``.
    """
    if value is None:
        return []
    v = value.strip()
    if not v:
        return []
    if v.startswith(DELIM):
        v = v[len(DELIM) :]
    if v.endswith(DELIM):
        v = v[: -len(DELIM)]
    if not v:
        return []
    return v.split(DELIM)


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def decode_text(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> str | None:
    """``Text``/``Note``. Note values may contain HTML; preserved verbatim."""
    if value is None:
        return None
    return value


def decode_number(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> float | None:
    """``Number``/``Currency``: ``1234.500000000000``."""
    if _is_blank(value):
        return None
    try:
        return float(str(value).strip())
    except ValueError:
        ctx.warn("decode.number_unparseable", raw=value)
        return None


def decode_int(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> int | None:
    """``Counter``/``Integer``."""
    if _is_blank(value):
        return None
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        try:  # some integer columns arrive as "42.0000000000"
            return int(float(text))
        except ValueError:
            ctx.warn("decode.int_unparseable", raw=value)
            return None


def decode_boolean(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> bool | None:
    """``Boolean``: ``1``/``0``, occasionally ``TRUE``/``FALSE``."""
    if value is None:
        return None
    token = str(value).strip().lower()
    if token == "":
        return None
    if token in TRUE_TOKENS:
        return True
    if token in FALSE_TOKENS:
        return False
    ctx.warn("decode.boolean_unparseable", raw=value)
    return None


def decode_datetime(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> datetime | None:
    """``DateTime``. With ``DateInUtc=TRUE`` the wire form is ``…THH:MM:SSZ``.

    Values without a zone designator are *assumed* UTC, which is what
    ``DateInUtc`` claims. ``spconnect verify-time`` exists because that claim
    has to be checked against the real server once.
    """
    if _is_blank(value):
        return None
    text = str(value).strip()
    for pattern in _DATE_PATTERNS:
        try:
            parsed = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        ctx.warn("decode.datetime_unparseable", raw=value)
        return None


def decode_multichoice(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> list[str]:
    """``MultiChoice``: ``;#Reparatur;#Garantie;#``."""
    if _is_blank(value):
        return []
    return [token for token in split_multi(value) if token != ""]


def decode_choice(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> str | None:
    if value is None:
        return None
    # Some builds emit single-choice values with the multi wrapper anyway.
    if value.startswith(DELIM) and value.endswith(DELIM):
        tokens = decode_multichoice(value, ctx)
        return tokens[0] if tokens else None
    return value


def _pair(tokens: list[str]) -> dict[str, Any]:
    raw_id, raw_value = tokens[0], tokens[1]
    try:
        item_id: int | None = int(raw_id)
    except ValueError:
        return {"id": None, "value": DELIM.join(tokens)}
    return {"id": item_id, "value": raw_value if raw_value != "" else None}


def decode_lookup(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> dict[str, Any] | None:
    """``Lookup``: ``42;#Müller Maschinenbau GmbH``.

    The numeric id is the durable part. The display string is a denormalised
    snapshot written when the row was last saved and may already be stale.
    """
    if _is_blank(value):
        return None
    tokens = split_multi(value)
    if not tokens:
        return None
    if len(tokens) == 1:
        token = tokens[0]
        if token.isdigit():
            return {"id": int(token), "value": None}
        ctx.warn("decode.lookup_without_id", raw=value)
        return {"id": None, "value": token}
    if len(tokens) > 2:
        # Either a display value that itself contains ";#", or a multi value in
        # a single-value column. Keep the id, rejoin the rest, and shout.
        ctx.warn("decode.lookup_odd_tokens", raw=value, tokens=len(tokens))
        return _pair([tokens[0], DELIM.join(tokens[1:])])
    return _pair(tokens)


def decode_lookup_multi(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> list[dict[str, Any]]:
    """``LookupMulti``: ``42;#Müller;#57;#Beta AG``."""
    if _is_blank(value):
        return []
    tokens = split_multi(value)
    if not tokens:
        return []
    if len(tokens) % 2 != 0:
        ctx.warn("decode.lookup_multi_odd_tokens", raw=value, tokens=len(tokens))
        tokens = tokens[: len(tokens) - 1]
    return [_pair([tokens[i], tokens[i + 1]]) for i in range(0, len(tokens), 2)]


def decode_user(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> dict[str, Any] | None:
    """``User``: ``12;#CONTOSO\\jdoe``. Same wire encoding as ``Lookup``."""
    return decode_lookup(value, ctx)


def decode_user_multi(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> list[dict[str, Any]]:
    """``UserMulti``."""
    return decode_lookup_multi(value, ctx)


def decode_url(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> dict[str, Any] | None:
    """``URL``: ``http://example.com, Anzeigetext``."""
    if _is_blank(value):
        return None
    text = str(value)
    if ", " in text:
        url, _, description = text.partition(", ")
        return {"url": url.strip(), "description": description.strip() or None}
    return {"url": text.strip(), "description": None}


def decode_attachments(value: str, ctx: DecodeContext = _NULL_CONTEXT) -> bool | None:
    """``Attachments``: ``1``/``0``. On document libraries it can be a URL."""
    if value is None:
        return None
    token = str(value).strip()
    if token in ("0", "1", ""):
        return token == "1"
    return True


def decode_attachment_urls(value: str | None) -> list[str]:
    """``ows_AttachmentUrls`` — ``;#``-separated absolute URLs."""
    if _is_blank(value):
        return []
    return [token.strip() for token in split_multi(str(value)) if token.strip()]


def decode_fsobjtype(value: str | None) -> int | None:
    """``ows_FSObjType`` arrives bare (``0``) or lookup-prefixed (``1;#0``)."""
    if _is_blank(value):
        return None
    tokens = split_multi(str(value))
    if not tokens:
        return None
    candidate = tokens[-1]
    try:
        return int(candidate)
    except ValueError:
        return None


_CALCULATED_DECODERS: dict[str, Callable[[str, DecodeContext], Any]] = {
    "float": decode_number,
    "number": decode_number,
    "currency": decode_number,
    "double": decode_number,
    "int": decode_int,
    "integer": decode_int,
    "counter": decode_int,
    "datetime": decode_datetime,
    "date": decode_datetime,
    "boolean": decode_boolean,
    "bool": decode_boolean,
    "string": decode_text,
    "text": decode_text,
    "note": decode_text,
    "lookup": decode_text,
    "choice": decode_text,
}


def decode_calculated(
    value: str,
    ctx: DecodeContext = _NULL_CONTEXT,
    result_type: str | None = None,
) -> Any:
    """``Calculated``: ``float;#1234.5`` — the value prefixed with its own type."""
    if _is_blank(value):
        return None
    text = str(value)
    if DELIM in text:
        prefix, _, rest = text.partition(DELIM)
        decoder = _CALCULATED_DECODERS.get(prefix.strip().lower())
        if decoder is not None:
            return decoder(rest, ctx)
        ctx.warn("decode.calculated_unknown_prefix", raw=value, prefix=prefix)
        return rest
    if result_type:
        decoder = _CALCULATED_DECODERS.get(result_type.strip().lower())
        if decoder is not None:
            return decoder(text, ctx)
    return text


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #

_SIMPLE_DECODERS: dict[str, Callable[[str, DecodeContext], Any]] = {
    "Text": decode_text,
    "Note": decode_text,
    "Guid": decode_text,
    # ``ows_FileLeafRef`` is typed ``File`` in the schema but arrives lookup-encoded
    # as ``1;#name.pdf``. Decoding it as a lookup keeps it consistent with FileRef.
    "File": decode_lookup,
    "ContentTypeId": decode_text,
    "Number": decode_number,
    "Currency": decode_number,
    "Counter": decode_int,
    "Integer": decode_int,
    "Boolean": decode_boolean,
    "AllDayEvent": decode_boolean,
    "Attachments": decode_attachments,
    "DateTime": decode_datetime,
    "Choice": decode_choice,
    "MultiChoice": decode_multichoice,
    "GridChoice": decode_multichoice,
    "Lookup": decode_lookup,
    "LookupMulti": decode_lookup_multi,
    "User": decode_user,
    "UserMulti": decode_user_multi,
    "URL": decode_url,
}


def decode_value(
    field_type: str,
    value: str | None,
    ctx: DecodeContext = _NULL_CONTEXT,
    field: FieldDef | None = None,
) -> Any:
    """Decode one wire value given its schema ``Type``.

    Unknown types fall back to the raw string — never an exception, never a
    silent ``None``.
    """
    if value is None:
        return None
    if field_type == "Calculated":
        return decode_calculated(value, ctx, result_type=field.result_type if field else None)
    decoder = _SIMPLE_DECODERS.get(field_type)
    if decoder is None:
        return decode_text(value, ctx)
    return decoder(value, ctx)


class RowDecoder:
    """Decodes whole ``<z:row>`` attribute dicts for one list."""

    def __init__(
        self,
        fields: dict[str, FieldDef] | None = None,
        *,
        list_title: str = "",
        list_guid: str = "",
        web_url: str = "",
        on_warning: WarnFn | None = None,
    ) -> None:
        self.fields = fields or {}
        self.ctx = DecodeContext(
            list_title=list_title, list_guid=list_guid, web_url=web_url, on_warning=on_warning
        )

    @property
    def warning_count(self) -> int:
        return self.ctx.warning_count

    def field_type(self, internal_name: str) -> tuple[str, FieldDef | None]:
        field = self.fields.get(internal_name)
        if field is not None:
            return field.type, field
        return SYSTEM_FIELD_TYPES.get(internal_name, "Text"), None

    def decode_row(self, raw: dict[str, str]) -> dict[str, Any]:
        """Map ``{"ows_Title": "..."} -> {"Title": <decoded>}``.

        Keys are internal names, still in their ``_xHHHH_`` escaped form —
        that is the name the downstream pipeline joins on.
        """
        self.ctx.item_id = raw.get("ows_ID")
        decoded: dict[str, Any] = {}
        for key, value in raw.items():
            if not key.startswith(OWS_PREFIX):
                continue
            internal = key[len(OWS_PREFIX) :]
            if internal in SKIPPED_FIELDS:
                continue
            self.ctx.field_name = internal
            if internal == "FSObjType":
                decoded[internal] = decode_fsobjtype(value)
                continue
            if internal == "AttachmentUrls":
                decoded[internal] = decode_attachment_urls(value)
                continue
            field_type, field = self.field_type(internal)
            decoded[internal] = decode_value(field_type, value, self.ctx, field)
        self.ctx.field_name = ""
        self.ctx.item_id = None
        return decoded


def strip_ows(raw: dict[str, str]) -> dict[str, str]:
    """Raw attribute dict with the ``ows_`` prefix removed and MetaInfo dropped."""
    return {
        k[len(OWS_PREFIX) :]: v
        for k, v in raw.items()
        if k.startswith(OWS_PREFIX) and k[len(OWS_PREFIX) :] not in SKIPPED_FIELDS
    }


def json_default(value: Any) -> Any:
    """``json.dumps`` hook: datetimes go out as UTC ISO-8601 with a ``Z``."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serialisable")


_INT_RE = re.compile(r"^-?\d+$")


def coerce_item_id(raw: dict[str, str]) -> int | None:
    """Pull a usable integer item id out of a raw row."""
    value = raw.get("ows_ID")
    if value is None:
        return None
    text = str(value).strip()
    if _INT_RE.match(text):
        return int(text)
    tokens = split_multi(text)
    if tokens and _INT_RE.match(tokens[0]):
        return int(tokens[0])
    return None
