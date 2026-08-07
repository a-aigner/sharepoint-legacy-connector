"""``ListData.svc`` — the SharePoint 2010 OData/REST backend.

**Scope: item extraction only.** This is not an alternative to the SOAP layer,
it is a second source for one step of the crawl, because OData genuinely cannot
do the rest:

* **Web discovery** has no OData equivalent — ``Webs.asmx`` stays.
* **List discovery and field schema** stay on ``Lists.asmx``: we need list
  GUIDs, internal field names and the ``List=``/``ShowField`` lookup targets
  that make ``_graph.json``. ``$metadata`` exposes EDM associations between
  *sanitised entity-set names*, which is strictly less information.
* **Deletes** come from ``GetListItemChangesSinceToken``. OData has no change
  feed at all, so ``sync`` stays on SOAP.

What OData does give us is typed values — real numbers, booleans and dates —
which is worth being able to compare against the ``ows_`` decoder on real data.

Three documented traps drive the design:

1. Entity-set names are *derived* from list titles: spaces removed, words
   capitalised, non-ASCII mangled (``My Test ïist`` -> ``MyTestÏist``). We never
   guess one; we read the service document and match.
2. The default page size is 1000 regardless of ``$top``, so paging is
   mandatory. We page on ``Id`` exactly as the SOAP backend does, which keeps
   the same resumable checkpoints and stays index-seekable under the 5000-item
   threshold.
3. The service **document** need not be available as JSON. Feeds and entities
   render as JSON on request, but the document at the service root is AtomPub by
   definition and whether a given WCF Data Services build will emit JSON for it
   is version-dependent — the reported SharePoint 2010 farm serves ``.atom``
   there. So we ask for JSON, and read Atom when that is what arrives, rather
   than concluding the feature is missing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import requests
from lxml import etree

from ..config import get_logger
from ..models import ListSchema, normalise_url
from ..soap import find_all
from ..transport import AuthenticationError, NotFoundError, Transport, TransportError

log = get_logger(__name__)

#: ``/Date(1288323623006)/`` and ``/Date(1288323623006+0060)/``
_ODATA_DATE_RE = re.compile(r"^/Date\((-?\d+)(?:([+-])(\d{2})(\d{2}))?\)/$")

#: Keys the service adds to every entity that are not list data.
_CONTROL_KEYS = frozenset({"__metadata", "__deferred", "__next", "__count"})

_ATOM_NS = "http://www.w3.org/2005/Atom"
#: ``d:`` — where property values live.
_DS_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices"
#: ``m:`` — where the type, null and inline annotations live.
_DSM_NS = "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"

_ATOM_ENTRY = f"{{{_ATOM_NS}}}entry"
_ATOM_FEED = f"{{{_ATOM_NS}}}feed"
_ATOM_LINK = f"{{{_ATOM_NS}}}link"
_ATOM_CONTENT = f"{{{_ATOM_NS}}}content"
_M_PROPERTIES = f"{{{_DSM_NS}}}properties"
_M_INLINE = f"{{{_DSM_NS}}}inline"
_M_TYPE = f"{{{_DSM_NS}}}type"
_M_NULL = f"{{{_DSM_NS}}}null"

#: ``rel`` prefix marking a navigation property rather than an ordinary Atom link.
_RELATED = "/related/"

#: EDM types that become Python ints. **Not** ``Edm.Int64``: OData's verbose JSON
#: sends 64-bit and decimal values as *strings* to avoid float precision loss, and
#: :class:`ODataRowMapper` coerces those through the SOAP schema. Parsing them to
#: numbers here would make the two backends disagree on every currency column.
_EDM_INT = frozenset({"Edm.Int16", "Edm.Int32", "Edm.Byte", "Edm.SByte"})
_EDM_FLOAT = frozenset({"Edm.Double", "Edm.Single"})

#: JSON is preferred where a farm offers it — it needs no type table and is what
#: the landing-zone contract was defined against — but Atom is what OData v2
#: *requires* a service to speak, so it is always acceptable. Asking for both in
#: one header keeps this to a single request on either kind of farm, with no
#: negotiation state to remember and nothing to get stale between webs.
_ACCEPT_ENTITIES = "application/json;q=1.0, application/atom+xml;q=0.9, application/xml;q=0.8"
_ACCEPT_SERVICE_DOCUMENT = "application/json;q=1.0, application/atomsvc+xml;q=0.9, application/xml;q=0.8"


def _local(tag: Any) -> str:
    """Local name of an element tag, ignoring its namespace."""
    return str(tag).rsplit("}", 1)[-1]


def parse_atom_value(el: etree._Element) -> Any:
    """One ``<d:Property m:type="...">`` element as the JSON backend would send it.

    The target is deliberately *verbose-JSON parity* rather than the most natural
    Python value, because :class:`ODataRowMapper` and the landing-zone contract
    are already defined against the JSON shape. Anything else here would make the
    two backends produce different output for the same row, which is the one thing
    having two backends is supposed to let us rule out.
    """
    if (el.get(_M_NULL) or "").strip().lower() == "true":
        return None

    edm = el.get(_M_TYPE) or "Edm.String"

    # A property with child elements is a collection — a multi-choice or
    # multi-lookup column. Verbose JSON wraps those in {"results": [...]}, so the
    # children's text goes into the same shape whatever the wrapper is called.
    children = list(el)
    if children:
        return {"results": [child.text or "" for child in children]}

    text = el.text or ""
    if edm == "Edm.Boolean":
        return text.strip().lower() == "true"
    if edm in _EDM_INT:
        try:
            return int(text.strip())
        except ValueError:
            log.warning("odata.atom_int_unparseable", property=_local(el.tag), raw=text[:50])
            return text
    if edm in _EDM_FLOAT:
        try:
            return float(text.strip())
        except ValueError:
            log.warning("odata.atom_float_unparseable", property=_local(el.tag), raw=text[:50])
            return text
    # Everything else stays a string, Edm.DateTime included: ODataRowMapper runs
    # parse_odata_datetime over it, which reads Atom's ISO form and JSON's
    # /Date(ms)/ form to the same instant.
    return text


def _own_properties(entry: etree._Element) -> etree._Element | None:
    """The ``m:properties`` belonging to *this* entry.

    Direct-child lookups only, and never a descendant search: an expanded
    navigation property carries a whole nested ``<entry>`` with properties of its
    own, and a descendant search would merge a customer's columns into the service
    case that referenced it.

    Two valid positions — under ``content`` for an ordinary entry, and as a
    sibling of it for a media-link entry, which is what a document library's rows
    are.
    """
    content = entry.find(_ATOM_CONTENT)
    if content is not None:
        found = content.find(_M_PROPERTIES)
        if found is not None:
            return found
    return entry.find(_M_PROPERTIES)


def atom_entry_to_dict(entry: etree._Element) -> dict[str, Any]:
    """One ``<entry>`` as the dict the JSON backend would have produced."""
    row: dict[str, Any] = {}

    properties = _own_properties(entry)
    if properties is not None:
        for prop in properties:
            row[_local(prop.tag)] = parse_atom_value(prop)

    # Navigation properties. Direct-child links only, for the same reason as above.
    for link in entry.findall(_ATOM_LINK):
        rel = link.get("rel") or ""
        if _RELATED not in rel:
            continue
        name = rel.rsplit(_RELATED, 1)[1]
        if not name:
            continue
        inline = link.find(_M_INLINE)
        if inline is None:
            # Unexpanded, exactly as JSON reports it. ODataRowMapper skips these;
            # emitting the same marker keeps the raw record identical too.
            row[name] = {"__deferred": {"uri": link.get("href") or ""}}
            continue
        nested_feed = inline.find(_ATOM_FEED)
        if nested_feed is not None:
            rows, _ = parse_atom_feed(nested_feed)
            row[name] = {"results": rows}
            continue
        nested_entry = inline.find(_ATOM_ENTRY)
        if nested_entry is not None:
            row[name] = atom_entry_to_dict(nested_entry)
    return row


def parse_atom_feed(feed: etree._Element) -> tuple[list[dict[str, Any]], str | None]:
    """``(rows, next_link)`` from an OData v2 Atom feed.

    ``findall`` rather than ``xpath``: both the entries and the ``rel="next"``
    link must come from this feed and not from one inlined inside an entry.
    """
    rows = [atom_entry_to_dict(entry) for entry in feed.findall(_ATOM_ENTRY)]
    next_link = next(
        (link.get("href") for link in feed.findall(_ATOM_LINK) if (link.get("rel") or "") == "next"),
        None,
    )
    return rows, next_link


class ODataError(TransportError):
    """``ListData.svc`` answered, but not usefully."""


class ODataUnavailable(ODataError):
    """The service is absent, disabled, or broken on this web."""


class ODataNotJson(ODataError):
    """The service claimed JSON and did not deliver it.

    Deliberately **not** a subclass of :class:`ODataUnavailable`: a body that is
    not JSON is not evidence that the feature is missing, and conflating the two
    is how a farm serving Atom got reported as needing WCF Data Services
    installed. Subclasses :class:`ODataError` so the crawler's fall-back-to-SOAP
    path still catches it.
    """


class ODataNotAtom(ODataUnavailable):
    """Neither JSON nor parseable XML.

    Unlike :class:`ODataNotJson` this *is* an :class:`ODataUnavailable`. OData v2
    requires a service to speak Atom, so a body that is neither representation is
    not the service answering in an inconvenient format — it is something else
    answering in the service's place, which is what "absent, disabled, or broken"
    means.
    """


def normalise_name(name: str, *, ascii_only: bool = False) -> str:
    """Fold a list title or property name for matching.

    Entity-set naming is lossy and version-dependent, so matching is done on a
    normalised form rather than by reimplementing Microsoft's sanitiser.
    """
    if ascii_only:
        return re.sub(r"[^0-9a-zA-Z]", "", name).casefold()
    return "".join(ch for ch in name if ch.isalnum()).casefold()


def parse_odata_datetime(value: str) -> datetime | None:
    """``/Date(1288323623006)/`` -> timezone-aware UTC ``datetime``.

    Also accepts the ISO form the Atom representation uses, so the same decoder
    works whichever serialisation the farm hands back.
    """
    if not value:
        return None
    match = _ODATA_DATE_RE.match(value.strip())
    if match:
        millis = int(match.group(1))
        moment = datetime.fromtimestamp(millis / 1000, tz=UTC)
        if match.group(2):
            offset = timedelta(hours=int(match.group(3)), minutes=int(match.group(4)))
            moment = moment - offset if match.group(2) == "+" else moment + offset
        return moment
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class ODataPage:
    """One page of entities, shaped like :class:`~spconnect.services.lists.ItemPage`."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    next_link: str | None = None

    @property
    def max_id(self) -> int | None:
        ids: list[int] = [r["Id"] for r in self.rows if isinstance(r.get("Id"), int)]
        return max(ids) if ids else None


class ODataService:
    """``{web}/_vti_bin/ListData.svc`` for one web."""

    def __init__(self, transport: Transport, web_url: str) -> None:
        self.transport = transport
        self.web_url = normalise_url(web_url)
        self._entity_sets: list[str] | None = None
        #: ``"json"`` or ``"atom"`` once anything has been read. Worth reporting:
        #: it is the difference between a farm this connector had to be taught to
        #: read and one it always could, and the operator cannot see it otherwise.
        self.representation: str | None = None

    @property
    def endpoint(self) -> str:
        return f"{self.web_url}/_vti_bin/ListData.svc"

    # ---- raw requests ----

    def _get(self, url: str, *, accept: str) -> requests.Response:
        """One GET with ListData.svc's failure modes named. Never returns a >=400."""
        try:
            response = self.transport.request("GET", url, headers={"Accept": accept})
        except AuthenticationError:
            raise  # a bad credential is not an OData problem
        except NotFoundError as exc:
            raise ODataUnavailable(
                f"{url}: 404 — ListData.svc is absent or the OData feature is disabled on this web"
            ) from exc
        if response.status_code >= 400:
            raise ODataError(f"HTTP {response.status_code} for {url}: {response.text[:300]}")
        return response

    @staticmethod
    def _is_json(response: requests.Response) -> bool:
        """Which of the two representations came back.

        Content type first, then the first non-space byte. The sniff matters: a
        farm that ignores ``Accept`` entirely still has to be read correctly, and
        ``{`` versus ``<`` is not an ambiguous distinction between these two.
        """
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "json" in content_type:
            return True
        if "xml" in content_type or "atom" in content_type:
            return False
        return response.content.lstrip()[:1] == b"{"

    def _parse_json(self, url: str, response: requests.Response) -> dict[str, Any]:
        try:
            payload = json.loads(response.content.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise ODataNotJson(
                f"{url} claimed to return JSON ({response.headers.get('Content-Type')}) "
                f"but did not; first 200 bytes: {response.content[:200]!r}"
            ) from exc
        if not isinstance(payload, dict) or "d" not in payload:
            raise ODataNotJson(f"{url}: response had no 'd' envelope")
        return payload

    def _parse_atom(self, url: str, response: requests.Response) -> etree._Element:
        try:
            return etree.fromstring(response.content)
        except etree.XMLSyntaxError as exc:
            raise ODataNotAtom(
                f"{url} returned neither JSON nor well-formed XML "
                f"({response.headers.get('Content-Type')}); "
                f"first 200 bytes: {response.content[:200]!r}"
            ) from exc

    @staticmethod
    def _results(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Unwrap ``{"d": {"results": [...], "__next": "..."}}`` and its variants."""
        body = payload["d"]
        if isinstance(body, list):
            return body, None
        if isinstance(body, dict):
            results = body.get("results")
            if isinstance(results, list):
                return results, body.get("__next")
            return [body], None
        return [], None

    # ---- discovery ----

    def entity_sets(self) -> list[str]:
        """Entity-set names this web exposes, read rather than guessed.

        JSON first, then the AtomPub service document. Feeds and entities below
        this level render as JSON on request, but the service *document* is
        AtomPub by definition and whether a given WCF Data Services build will
        emit JSON for it at all is version-dependent — SharePoint 2010's need
        not, and the reported farm serves ``.atom`` here. Taking that for "the
        feature is absent" writes off a farm that has been serving REST the whole
        time, and sends the diagnosis after WCF Data Services rather than after
        the content type we asked for.
        """
        if self._entity_sets is None:
            # Bare `…/ListData.svc`, with no trailing slash and nothing appended.
            # The slash is optional in the OData spec and not free in practice:
            # IIS of this vintage can treat `ListData.svc/` as a path below the
            # handler and answer 404 for a service that is running perfectly well.
            url = self.endpoint
            response = self._get(url, accept=_ACCEPT_SERVICE_DOCUMENT)
            if self._is_json(response):
                self.representation = "json"
                names = self._service_document_json(url, response)
            else:
                self.representation = "atom"
                names = self._service_document_atom(response)
            if not names:
                raise ODataUnavailable(
                    f"{url} answered, but named no collections "
                    f"({response.headers.get('Content-Type')}); "
                    f"first 200 bytes: {response.content[:200]!r}\n"
                    "  A 404 would mean the feature is not installed. This is something "
                    "else answering in its place."
                )
            self._entity_sets = names
            log.info(
                "odata.service_document",
                web=self.web_url,
                entity_sets=len(names),
                representation=self.representation,
            )
        return self._entity_sets

    def _service_document_json(self, url: str, response: requests.Response) -> list[str]:
        payload = self._parse_json(url, response)
        rows, _ = self._results(payload)
        names = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str):
                names.append(name)
        if not names and isinstance(payload["d"], dict):
            names = [k for k in payload["d"] if k not in _CONTROL_KEYS]
        return names

    def _service_document_atom(self, response: requests.Response) -> list[str]:
        """Read ``<collection href="...">`` out of an AtomPub service document.

        ``href`` rather than ``atom:title``: it is the URL segment every later
        request is built from. The two usually agree, and where a sanitiser has
        made them disagree the one that routes is the one we want.

        Namespace-agnostic via :func:`~spconnect.soap.find_all`, because the
        document mixes the ``app`` and ``atom`` namespaces and older builds
        disagree about which one is the default.
        """
        root = self._parse_atom(self.endpoint, response)
        return [href for c in find_all(root, "collection") if (href := c.get("href"))]

    def available(self) -> tuple[bool, str | None]:
        """Is ListData.svc usable on this web? Never raises."""
        try:
            sets = self.entity_sets()
        except Exception as exc:
            return False, str(exc)
        return True, f"{len(sets)} collection(s)"

    def entity_set_for(self, list_title: str) -> str | None:
        """Map a list title to its entity-set name via the service document.

        Tries a unicode-preserving fold first, then an ASCII-only fold, because
        whether umlauts survive the server's sanitiser is build-dependent.
        """
        try:
            candidates = self.entity_sets()
        except ODataError:
            return None
        for ascii_only in (False, True):
            target = normalise_name(list_title, ascii_only=ascii_only)
            matches = [n for n in candidates if normalise_name(n, ascii_only=ascii_only) == target]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                log.warning("odata.ambiguous_entity_set", list=list_title, matches=matches)
                return matches[0]
        log.warning("odata.no_entity_set", list=list_title, available=len(candidates))
        return None

    # ---- counting ----

    def count(self, entity_set: str) -> int:
        """How many entities this collection holds.

        ``$count`` first: a path segment that answers with a bare integer in
        ``text/plain``, so it costs one request and needs no parsing at all.
        Where a build or a proxy in front of it does not implement that,
        ``$inlinecount=allpages`` asks for the same number inside an otherwise
        empty page, which every OData v2 service can render in either
        representation.

        **Raises rather than returning a sentinel.** A count that could not be
        taken must never be indistinguishable from a count of zero — an empty
        list and an unreadable one call for opposite responses, and a caller
        given ``0`` for both has no way to tell which it got.
        """
        url = f"{self.endpoint}/{entity_set}/$count"
        try:
            response = self._get(url, accept="text/plain")
            text = response.content.decode("utf-8", "replace").strip()
            if text.isdigit():
                return int(text)
            # A service without $count can answer 200 with a whole page instead
            # of a number, so the digits are the confirmation and the status is
            # not. Falling through to the query option is the right move either
            # way, and it is one more request rather than a wrong answer.
            raise ODataError(f"{url} answered without a count: {text[:100]!r}")
        except ODataError as exc:
            log.debug("odata.count.fallback", entity_set=entity_set, detail=str(exc).splitlines()[0])
            return self._count_via_inlinecount(entity_set)

    def _count_via_inlinecount(self, entity_set: str) -> int:
        url = f"{self.endpoint}/{entity_set}?$top=0&$inlinecount=allpages"
        response = self._get(url, accept=_ACCEPT_ENTITIES)
        raw: Any = None
        if self._is_json(response):
            body = self._parse_json(url, response)["d"]
            if isinstance(body, dict):
                raw = body.get("__count")
        else:
            found = find_all(self._parse_atom(url, response), "count")
            raw = found[0].text if found else None
        text = str(raw).strip() if raw is not None else ""
        if not text.isdigit():
            raise ODataError(f"{url} returned no usable $inlinecount (got {raw!r})")
        return int(text)

    # ---- items ----

    def get_items(
        self,
        entity_set: str,
        *,
        last_id: int = 0,
        top: int = 200,
        expand: list[str] | None = None,
    ) -> ODataPage:
        """One page, paged on ``Id`` so checkpoints match the SOAP backend."""
        query = [
            f"$filter=Id%20gt%20{int(last_id)}",
            "$orderby=Id",
            f"$top={int(top)}",
        ]
        if expand:
            query.append("$expand=" + ",".join(expand))
        url = f"{self.endpoint}/{entity_set}?" + "&".join(query)
        log.debug("odata.query", entity_set=entity_set, last_id=last_id, top=top, url=url)

        response = self._get(url, accept=_ACCEPT_ENTITIES)
        if self._is_json(response):
            self.representation = "json"
            rows, next_link = self._results(self._parse_json(url, response))
        else:
            self.representation = "atom"
            rows, next_link = parse_atom_feed(self._parse_atom(url, response))
        page = ODataPage(rows=[r for r in rows if isinstance(r, dict)], next_link=next_link)
        log.debug("odata.page", entity_set=entity_set, rows=len(page.rows), max_id=page.max_id)
        return page


class ODataRowMapper:
    """Turns OData entities into the landing zone's contract.

    The contract is keyed on **internal field names** from the SOAP schema, so
    the mapper's whole job is bridging sanitised OData property names back to
    them. Anything it cannot map is kept under its OData name rather than
    dropped — losing a column silently is the one unacceptable outcome.
    """

    #: OData v2 serialises Edm.Decimal/Int64 as *strings* to avoid float
    #: precision loss. The SOAP backend yields numbers for the same columns, so
    #: coerce using the schema or the two backends disagree on every currency.
    _NUMERIC_TYPES = frozenset({"Number", "Currency"})
    _INTEGER_TYPES = frozenset({"Integer", "Counter"})

    def __init__(self, schema: ListSchema) -> None:
        self.schema = schema
        self._by_normalised: dict[str, str] = {}
        self._fields = schema.field_map()
        self.unmapped: set[str] = set()

        from ..schema import unescape_internal_name

        for fielddef in schema.fields:
            for candidate in (
                fielddef.display_name,
                unescape_internal_name(fielddef.name),
                fielddef.name,
            ):
                if not candidate:
                    continue
                for ascii_only in (False, True):
                    key = normalise_name(candidate, ascii_only=ascii_only)
                    self._by_normalised.setdefault(key, fielddef.name)

    def internal_name(self, odata_property: str) -> str | None:
        for ascii_only in (False, True):
            hit = self._by_normalised.get(normalise_name(odata_property, ascii_only=ascii_only))
            if hit:
                return hit
        return None

    def map_row(self, entity: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(raw, decoded)`` — the entity as received, and contract-shaped."""
        raw = {k: v for k, v in entity.items() if k not in _CONTROL_KEYS}
        decoded: dict[str, Any] = {}
        lookup_ids: dict[str, int] = {}

        for key, value in raw.items():
            if isinstance(value, dict) and "__deferred" in value:
                continue  # an unexpanded navigation property carries no data
            # ListData.svc exposes a lookup as both `Kunde` and the scalar `KundeId`.
            if key.endswith("Id") and isinstance(value, int) and len(key) > 2:
                base = self.internal_name(key[:-2])
                if base is not None:
                    lookup_ids[base] = value
                    continue
            target = self.internal_name(key)
            decoded[target or key] = self._coerce(target, self._convert(value))
            if target is None and key not in ("Id",):
                self.unmapped.add(key)

        for internal, item_id in lookup_ids.items():
            existing = decoded.get(internal)
            if isinstance(existing, dict):
                existing.setdefault("id", item_id)
            elif isinstance(existing, str) or existing is None:
                # Keep the id: it is the durable half of a lookup, the label is
                # a denormalised snapshot.
                decoded[internal] = {"id": item_id, "value": existing}

        decoded.setdefault("ID", entity.get("Id"))
        return raw, decoded

    def _coerce(self, internal_name: str | None, value: Any) -> Any:
        """Align OData's types with the ``ows_`` decoder's for the same column."""
        if internal_name is None or not isinstance(value, str):
            return value
        fielddef = self._fields.get(internal_name)
        if fielddef is None:
            return value
        try:
            if fielddef.type in self._NUMERIC_TYPES:
                return float(value)
            if fielddef.type in self._INTEGER_TYPES:
                return int(float(value))
        except ValueError:
            log.warning("odata.numeric_unparseable", field=internal_name, raw=value)
        return value

    def _convert(self, value: Any) -> Any:
        if isinstance(value, str):
            parsed = parse_odata_datetime(value)
            return parsed if parsed is not None else value
        if isinstance(value, dict):
            if "results" in value and isinstance(value["results"], list):
                return [self._convert(v) for v in value["results"]]
            inner = {k: v for k, v in value.items() if k not in _CONTROL_KEYS}
            if "Id" in inner:
                return {
                    "id": inner.get("Id"),
                    "value": inner.get("Title") or inner.get("Value") or inner.get("Name"),
                }
            return {k: self._convert(v) for k, v in inner.items()}
        if isinstance(value, list):
            return [self._convert(v) for v in value]
        return value
