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


class ODataError(TransportError):
    """``ListData.svc`` answered, but not usefully."""


class ODataUnavailable(ODataError):
    """The service is absent, disabled, or broken on this web."""


class ODataNotJson(ODataError):
    """The service answered, but not as JSON. It may still speak Atom.

    Deliberately **not** a subclass of :class:`ODataUnavailable`: a body that is
    not JSON is not evidence that the feature is missing, and conflating the two
    is how a farm serving Atom got reported as needing WCF Data Services
    installed. Subclasses :class:`ODataError` so the crawler's fall-back-to-SOAP
    path still catches it.
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

    @property
    def endpoint(self) -> str:
        return f"{self.web_url}/_vti_bin/ListData.svc"

    # ---- raw requests ----

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            response = self.transport.request("GET", url, headers={"Accept": "application/json"})
        except AuthenticationError:
            raise  # a bad credential is not an OData problem
        except NotFoundError as exc:
            raise ODataUnavailable(
                f"{url}: 404 — ListData.svc is absent or the OData feature is disabled on this web"
            ) from exc
        if response.status_code >= 400:
            raise ODataError(f"HTTP {response.status_code} for {url}: {response.text[:300]}")
        try:
            payload = json.loads(response.content.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            # Atom, or an error page from a farm without the feature. Which of
            # those it is cannot be told from here, so say what arrived and let
            # the caller decide whether Atom is worth a try.
            raise ODataNotJson(
                f"{url} did not return JSON ({response.headers.get('Content-Type')}); "
                f"first 200 bytes: {response.content[:200]!r}"
            ) from exc
        if not isinstance(payload, dict) or "d" not in payload:
            raise ODataNotJson(f"{url}: response had no 'd' envelope")
        return payload

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
            try:
                names = self._entity_sets_from_json()
            except ODataNotJson as exc:
                log.info(
                    "odata.service_document.not_json",
                    web=self.web_url,
                    detail="service document is not JSON; reading it as AtomPub instead",
                    content=str(exc).split(";", 1)[0],
                )
                names = self._entity_sets_from_atom(json_error=exc)
            self._entity_sets = names
            log.info("odata.service_document", web=self.web_url, entity_sets=len(names))
        return self._entity_sets

    def _entity_sets_from_json(self) -> list[str]:
        payload = self._get_json(self.endpoint + "/")
        rows, _ = self._results(payload)
        names = []
        for row in rows:
            name = row.get("name") if isinstance(row, dict) else None
            if isinstance(name, str):
                names.append(name)
        if not names and isinstance(payload["d"], dict):
            names = [k for k in payload["d"] if k not in _CONTROL_KEYS]
        return names

    def _entity_sets_from_atom(self, *, json_error: ODataNotJson) -> list[str]:
        """Read ``<collection href="...">`` out of an AtomPub service document.

        ``href`` rather than ``atom:title``: it is the URL segment, which is what
        every later request is built from. The two usually agree, and where a
        sanitiser has made them disagree the one that routes is the one we want.

        Namespace-agnostic, because the document mixes the ``app`` and ``atom``
        namespaces and older builds disagree about which is the default.
        """
        url = self.endpoint + "/"
        response = self.transport.request(
            "GET", url, headers={"Accept": "application/atomsvc+xml, application/xml, text/xml"}
        )
        names: list[str] = []
        try:
            root = etree.fromstring(response.content)
        except etree.XMLSyntaxError:
            root = None
        if root is not None:
            for collection in find_all(root, "collection"):
                href = collection.get("href")
                if href:
                    names.append(href)

        if not names:
            raise ODataUnavailable(
                f"{url} returned neither JSON nor an Atom service document.\n"
                f"  as JSON: {json_error}\n"
                f"  as Atom: {response.headers.get('Content-Type')}, no <collection> elements in "
                f"{response.content[:200]!r}\n"
                "  A 404 would mean the feature is not installed; this is something else "
                "answering in its place."
            )
        return names

    def available(self) -> tuple[bool, str | None]:
        """Is ListData.svc usable on this web? Never raises."""
        try:
            sets = self.entity_sets()
        except Exception as exc:
            return False, str(exc)
        return True, f"{len(sets)} entity sets"

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

        payload = self._get_json(url)
        rows, next_link = self._results(payload)
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
