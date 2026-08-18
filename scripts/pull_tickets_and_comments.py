#!/usr/bin/env python3
"""Pull the Ticket list and its TicketComment list over ``ListData.svc``.

Standalone and SOAP-free. It borrows ``spconnect`` for the things that are
genuinely hard — NTLM over legacy TLS, retries, rate limiting, and Id-paged
OData reads that stay under SharePoint 2010's 5000-item list view threshold —
and adds nothing to the package itself.

Both lists are read the same way, because on the wire they are the same thing:
comments are an ordinary list whose rows carry a parent pointer back to a
ticket. There is no per-ticket request anywhere in here. ~15k tickets and
~100k comments cost roughly (15k + 100k) / page_size requests in total, not
one request per ticket.

Usage::

    python scripts/pull_tickets_and_comments.py --list-sets
    python scripts/pull_tickets_and_comments.py
    python scripts/pull_tickets_and_comments.py --limit 50      # smoke test

Resumable: rerunning continues from the highest Id already on disk.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import traceback
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spconnect.config import Settings, load_settings, setup_logging
from spconnect.console import format_bytes
from spconnect.services.odata import (
    ODataError,
    ODataNotJson,
    ODataService,
    ODataUnavailable,
    normalise_name,
    parse_atom_feed,
    parse_odata_datetime,
)
from spconnect.transport import (
    AuthenticationError,
    NotFoundError,
    RedirectRefused,
    RetryableTransportError,
    Transport,
    TransportError,
)

DEFAULT_TICKETS = "Ticket"
DEFAULT_COMMENTS = "TicketComment"


def note(message: str) -> None:
    """Progress goes to stderr so stdout stays pipeable."""
    print(message, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# entity sets
# --------------------------------------------------------------------------- #


def resolve_entity_set(service: ODataService, wanted: str, available: list[str]) -> str:
    """Find the collection actually named on the server.

    Entity-set names are derived from *list titles*, not list URLs — spaces
    removed, words capitalised, non-ASCII possibly mangled. ``/Lists/Ticket``
    is the URL; the collection could be ``Ticket``, ``Tickets`` or anything the
    title folds to. So an exact miss is not an answer, it is a prompt to look.
    """
    if wanted in available:
        return wanted

    lowered = {name.lower(): name for name in available}
    if wanted.lower() in lowered:
        return lowered[wanted.lower()]

    for ascii_only in (False, True):
        target = normalise_name(wanted, ascii_only=ascii_only)
        matches = [n for n in available if normalise_name(n, ascii_only=ascii_only) == target]
        if matches:
            return matches[0]

    near = [n for n in available if wanted.lower()[:6] in n.lower()]
    hint = f"  Closest by name: {', '.join(near)}\n" if near else ""
    raise SystemExit(
        f"\nNo collection named {wanted!r} on {service.endpoint}.\n"
        f"{hint}"
        f"  Available ({len(available)}): {', '.join(sorted(available))}\n\n"
        f"Pass the real name with --tickets / --comments."
    )


def resolve_optional_entity_set(service: ODataService, wanted: str, available: list[str]) -> str | None:
    """Like :func:`resolve_entity_set`, but absence is an answer rather than an exit.

    The ticket and comment collections are the job; the user list only puts names
    to the numeric ``CreatedById`` values. A farm that does not expose it should
    cost the author names, not the extraction.
    """
    try:
        return resolve_entity_set(service, wanted, available)
    except SystemExit:
        return None


# --------------------------------------------------------------------------- #
# query diagnosis
# --------------------------------------------------------------------------- #

ACCEPT_ENTITIES = "application/json;q=1.0, application/atom+xml;q=0.9, application/xml;q=0.8"


def error_message(body: bytes) -> str:
    """The server's own reason, dug out of whichever envelope it used.

    SharePoint states the real cause — a threshold, an unsortable column, a
    field it cannot serialise — inside a nested error document. Reporting the
    first 300 raw bytes instead usually shows only the envelope.
    """
    text = body.decode("utf-8", "replace").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        node: Any = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(node, dict):
            message = node.get("message")
            if isinstance(message, dict):
                message = message.get("value")
            if isinstance(message, str) and message.strip():
                return " ".join(message.split())
    # Atom/XML: <m:error><m:message xml:lang="…">reason</m:message></m:error>
    lowered = text.lower()
    start = lowered.find("<m:message")
    if start == -1:
        start = lowered.find("<message")
    if start != -1:
        opened = text.find(">", start)
        closed = lowered.find("</", opened)
        if opened != -1 and closed != -1:
            inner = text[opened + 1 : closed].strip()
            if inner:
                return " ".join(inner.split())
    return " ".join(text.split())[:300] or "(empty body)"


def probe(transport: Transport, url: str, *, language: str | None = None) -> tuple[int, str, bytes]:
    """One GET that reports rather than raises. Returns ``(status, detail, body)``.

    ``language`` overrides the session's ``Accept-Language`` for this request
    only, which is what lets the language comparison ask the same collection
    the same question in three languages.
    """
    headers = {"Accept": ACCEPT_ENTITIES}
    if language:
        headers["Accept-Language"] = language
    try:
        response = transport.request("GET", url, headers=headers)
    except AuthenticationError:
        return 401, "credential refused", b""
    except NotFoundError:
        return 404, "not found", b""
    except TransportError as exc:
        return 0, f"{type(exc).__name__}: {exc}", b""
    if response.status_code >= 400:
        return response.status_code, error_message(response.content), response.content
    size = len(response.content)
    detail = f"{size:,} bytes"
    if size <= 120:
        preview = response.content.decode("utf-8", "replace").strip()
        if preview:
            detail += f" — {preview}"
    return response.status_code, detail, response.content


def row_property_names(body: bytes) -> list[str]:
    """The property names on the first row of a feed, as the server spells them.

    This is the only authority that matters for ``$select``/``$filter``/
    ``$orderby``: OData property names are case-sensitive, and ``$metadata`` is
    a *description* of the model, which a farm can contradict. What comes back
    on the wire cannot.
    """
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        body_node = payload.get("d") if isinstance(payload, dict) else None
        if isinstance(body_node, dict):
            rows = body_node.get("results")
            body_node = rows[0] if isinstance(rows, list) and rows else body_node
        elif isinstance(body_node, list):
            body_node = body_node[0] if body_node else None
        return sorted(body_node) if isinstance(body_node, dict) else []

    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError:
        return []
    ns = "{http://schemas.microsoft.com/ado/2007/08/dataservices}"
    return sorted({etree.QName(el).localname for el in root.iter() if str(el.tag).startswith(ns)})


def deferred_property_names(body: bytes) -> list[str]:
    """Properties that came back as ``{"__deferred": {"uri": ...}}``.

    JSON only. The Atom representation expresses navigation as ``<link>``
    elements rather than as properties, so there is nothing to report there and
    an empty list is the honest answer.
    """
    text = body.decode("utf-8", "replace").strip()
    if not text.startswith("{"):
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    node = payload.get("d") if isinstance(payload, dict) else None
    if isinstance(node, dict):
        rows = node.get("results")
        node = rows[0] if isinstance(rows, list) and rows else node
    elif isinstance(node, list):
        node = node[0] if node else None
    if not isinstance(node, dict):
        return []
    return sorted(k for k, v in node.items() if isinstance(v, dict) and "__deferred" in v)


def next_link(body: bytes) -> str | None:
    """The server's own continuation for this feed, if it offered one.

    JSON verbose puts it at ``d.__next``; Atom uses ``<link rel="next">``. Its
    presence is what decides whether a list can be walked without ``$filter``
    or ``$orderby`` at all.
    """
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        node = payload.get("d") if isinstance(payload, dict) else None
        value = node.get("__next") if isinstance(node, dict) else None
        return value if isinstance(value, str) else None
    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError:
        return None
    for el in root.iter():
        if etree.QName(el).localname == "link" and el.get("rel") == "next":
            return el.get("href")
    return None


def report_property_names(body: bytes, wanted: str = "Id") -> None:
    """Say whether the property the queries depend on is really called that."""
    names = row_property_names(body)
    if not names:
        note("  could not read property names off the first row")
        return
    note(f"  {len(names)} propert(ies) on the first row, as the server spells them:")
    for start in range(0, len(names), 6):
        note("    " + ", ".join(names[start : start + 6]))
    if wanted in names:
        note(f"  '{wanted}' IS present — the query name is right and the refusal is about something else.")
        return
    variants = [n for n in names if n.lower() == wanted.lower()]
    if variants:
        note(f"  '{wanted}' is ABSENT; the server spells it {variants[0]!r}. OData names are case-sensitive.")
    else:
        note(f"  '{wanted}' is ABSENT, and no case variant exists either.")
        note("  Nothing here can be filtered or sorted as 'Id' — the paging key has to change.")


def diagnose(transport: Transport, service: ODataService, entity_set: str, page_size: int) -> None:
    """Bisect the query the pull uses, one option at a time.

    A 400 on the full query says the server refused *something*; it does not say
    what. Running the options separately does, and it costs seven requests.
    """
    base = f"{service.endpoint}/{entity_set}"
    ladder: list[tuple[str, str]] = [
        ("bare", "?$top=1"),
        ("$select=Id only", "?$select=Id&$top=1"),
        ("$orderby only", "?$orderby=Id&$top=1"),
        ("$filter only", "?$filter=Id%20gt%200&$top=1"),
        ("$filter + $orderby", "?$filter=Id%20gt%200&$orderby=Id&$top=1"),
        (f"full query, $top={page_size}", f"?$filter=Id%20gt%200&$orderby=Id&$top={page_size}"),
        # Counting, three ways. count() reaches for /$count first and only falls
        # back to $inlinecount, so probing the fallback alone tests the wrong
        # thing. And $top=1 alongside $top=0 separates "this service will not
        # count" from "this service will not accept $top=0", which are different
        # faults with the same symptom.
        ("/$count (tried first)", "/$count"),
        ("$inlinecount, $top=0", "?$top=0&$inlinecount=allpages"),
        ("$inlinecount, $top=1", "?$top=1&$inlinecount=allpages"),
        # No query options at all. If Id cannot be filtered or sorted on this
        # farm, this is the only remaining way to walk the list: take the
        # server's own default page and follow the continuation it hands back.
        ("server-driven paging (no options)", ""),
    ]

    note(f"\ndiagnosing entity set {entity_set!r} — {len(ladder)} requests")
    note(f"  base: {base}")
    note("")
    results: dict[str, int] = {}
    first_row = b""
    continuation: str | None = None
    for name, query in ladder:
        url = base + query
        status, detail, body = probe(transport, url)
        if name == "bare" and 200 <= status < 300:
            first_row = body
        if name.startswith("server-driven") and 200 <= status < 300:
            continuation = next_link(body)
            detail += " — continuation offered" if continuation else " — NO continuation link"
        results[name] = status
        verdict = "ok  " if 200 <= status < 300 else "FAIL"
        note(f"  {verdict} {status:>3}  {name}")
        # The URL belongs in the output, not behind -v: when a server denies a
        # property its own schema declares, the first thing to check is that the
        # request went where we think it did.
        note(f"            GET {url}")
        note(f"            {detail[:300]}")
    note("")

    if first_row:
        report_property_names(first_row)
        note("")

    ok = lambda name: 200 <= results.get(name, 0) < 300  # noqa: E731

    counts = ("/$count (tried first)", "$inlinecount, $top=0", "$inlinecount, $top=1")
    if not any(ok(name) for name in counts):
        note("  Counting is refused every way. That is cosmetic — the pull catches it")
        note("  and reports the total as unknown; it does not stop the extraction.")
    elif not ok("$inlinecount, $top=0") and ok("$inlinecount, $top=1"):
        note("  $inlinecount works but $top=0 does not: this build rejects a zero page")
        note("  rather than refusing to count. Also cosmetic.")
    note("")

    if ok("bare") and not ok("$select=Id only") and not ok("$filter only"):
        note("  VERDICT: the collection reads, but every query naming 'Id' is refused.")
        note("  Compare the property list above with what the queries ask for. If")
        note("  'Id' is not in it, $metadata and this service disagree and the")
        note("  service wins — check the metadata came from this same host.")
        if continuation:
            note("")
            note("  A continuation link WAS offered, so this list can be walked with no")
            note("  $filter and no $orderby: take the server's default page and follow")
            note("  __next until it stops. That is the fix, and it needs no key at all.")
        else:
            note("")
            note("  No continuation link was offered either, so server-driven paging")
            note("  cannot replace Id paging here. Whatever the server calls its key,")
            note("  in the property list above, is the only way through.")
    elif not ok("bare"):
        note("  VERDICT: the plainest possible read of this collection fails, so this")
        note("  is not about paging. If '$select=Id only' passed, one of the columns")
        note("  cannot be serialised by ListData.svc and $select is the way around it.")
    elif not ok("$orderby only"):
        note("  VERDICT: $orderby is what it refuses. On a list past the 5000-item")
        note("  threshold SharePoint rejects a sort it cannot satisfy from an index.")
        note("  ListData.svc already returns rows in Id order, so the sort can go.")
    elif not ok("$filter only"):
        note("  VERDICT: $filter is what it refuses — an encoding or syntax mismatch")
        note("  in 'Id gt N' rather than a threshold.")
    elif not ok(f"full query, $top={page_size}"):
        note(f"  VERDICT: the options pass individually but not combined at $top={page_size}.")
        note("  Retry with a smaller --page-size to separate size from combination.")
    else:
        note("  VERDICT: every query the pull uses passed here. The failure is")
        note("  page-dependent — rerun the pull with -v --log-bodies and compare the")
        note("  failing URL against these.")


# --------------------------------------------------------------------------- #
# the localised schema
# --------------------------------------------------------------------------- #
#
# ListData.svc derives OData property names from column *display* names, and
# display names are localised. The same farm therefore answers with `Id`,
# `Created`, `CreatedById` to one caller and `ID`, `Erstellt`, `ErstelltVonId`
# to another, depending on language. OData names are case-sensitive, so a
# hardcoded English name is not merely fragile — on a German web it refers to
# a property that does not exist, and the server says so with a 400.
#
# Nothing below matches a name literally. Each well-known column is resolved by
# role against the names the wire actually returned.

KEY_ALIASES = ("Id", "ID")
CREATED_ALIASES = ("Created", "Erstellt")
#: Foreign keys every list carries. They point at people or content types, never
#: at a parent row, and must not be mistaken for the link between two lists.
HOUSEKEEPING_FK_ALIASES = (
    "CreatedById",
    "ModifiedById",
    "ContentTypeID",
    "ErstelltVonId",
    "GeändertVonId",
    "InhaltstypID",
)


def resolve_property(names: Iterable[str], aliases: Sequence[str]) -> str | None:
    """The first alias this service actually uses, exact case preferred."""
    present = list(names)
    for alias in aliases:
        if alias in present:
            return alias
    lowered = {n.lower(): n for n in present}
    for alias in aliases:
        if alias.lower() in lowered:
            return lowered[alias.lower()]
    return None


@dataclass
class CollectionSchema:
    """What one collection calls the columns this script depends on."""

    entity_set: str
    properties: list[str]
    key: str | None
    created: str | None
    #: Navigation properties present only as ``__deferred`` URI stubs. These are
    #: exactly the fields $expand can turn into content, and the only ones worth
    #: spending an expansion on.
    deferred: list[str] = field(default_factory=list)

    def describe(self) -> str:
        stubs = f" {len(self.deferred)} stubs" if self.deferred else ""
        return (
            f"key={self.key or '-'} created={self.created or '-'} ({len(self.properties)} properties{stubs})"
        )


def discover_schema(
    transport: Transport,
    service: ODataService,
    entity_set: str,
    *,
    language: str | None = None,
) -> CollectionSchema:
    """One row, to learn what this service calls things. Costs one request."""
    status, detail, body = probe(transport, f"{service.endpoint}/{entity_set}?$top=1", language=language)
    if not (200 <= status < 300):
        raise ODataError(f"could not read a row from {entity_set}: {detail}")
    names = row_property_names(body)
    return CollectionSchema(
        entity_set=entity_set,
        properties=names,
        key=resolve_property(names, KEY_ALIASES),
        created=resolve_property(names, CREATED_ALIASES),
        deferred=deferred_property_names(body),
    )


def resolve_expansions(schema: CollectionSchema, spec: str | None) -> tuple[list[str], list[str]]:
    """Turn ``--expand`` into the list actually worth sending.

    Returns ``(wanted, unknown)``. Names are checked against the stubs the
    collection really has, because ``$expand`` on a property an entity does not
    define is a 400 for the whole page — one bad name would cost every row, not
    just that field.
    """
    if not spec:
        return [], []
    if spec.strip().lower() == "auto":
        return list(schema.deferred), []
    asked = [name.strip() for name in spec.split(",") if name.strip()]
    available = {name.lower(): name for name in schema.deferred}
    wanted = [available[name.lower()] for name in asked if name.lower() in available]
    unknown = [name for name in asked if name.lower() not in available]
    return wanted, unknown


@dataclass
class ExpansionPlan:
    """What to expand, per collection, plus names no collection recognised."""

    tickets: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def expansion_plan(
    tickets: CollectionSchema,
    comments: CollectionSchema,
    *,
    tickets_spec: str | None,
    comments_spec: str | None,
) -> ExpansionPlan:
    """Resolve expansions separately for the two collections.

    They behave nothing alike on this farm. The ticket list expanded at 42-53
    rows/s and completed; the same expansion on the comment list ran at 5 rows/s
    and exceeded the read timeout, because the server resolves a lookup per row
    and there are 102,625 comment rows against 14,729 tickets.

    Comments also have little to gain: their authors are 59 distinct users, which
    the user list answers in one pass rather than a hundred thousand lookups. So
    --expand drives tickets, and expanding comments has to be asked for.
    """
    ticket_names, ticket_unknown = resolve_expansions(tickets, tickets_spec)
    comment_names, comment_unknown = resolve_expansions(comments, comments_spec)
    return ExpansionPlan(
        tickets=ticket_names,
        comments=comment_names,
        unknown=list(dict.fromkeys(ticket_unknown + comment_unknown)),
    )


#: Asked of the same collection in --diagnose, to show what language negotiation
#: actually changes on this farm before anyone depends on it.
COMPARED_LANGUAGES: tuple[str | None, ...] = (None, "en-US", "de-DE")


def compare_languages(transport: Transport, service: ODataService, entity_set: str) -> None:
    """What each ``Accept-Language`` yields for the columns this script needs.

    SharePoint's MUI resolves *system* column display names from installed
    language packs, and ListData.svc derives OData property names from display
    names — so the same collection answers with ``Created`` or ``Erstellt``
    depending on who asks. Custom columns are not translated unless someone
    entered translations, so switching language moves some names and not
    others. Worth seeing rather than assuming.
    """
    note("  language negotiation — the same collection, asked in three languages")
    seen: list[tuple[str, CollectionSchema]] = []
    for language in COMPARED_LANGUAGES:
        label = language or "(as configured)"
        try:
            schema = discover_schema(transport, service, entity_set, language=language)
        except ODataError as exc:
            note(f"    {label:<17} failed: {exc}")
            continue
        seen.append((label, schema))
        key_name = schema.key or "-"
        created_name = schema.created or "-"
        note(
            f"    {label:<17} key={key_name:<8} created={created_name:<10} "
            f"{len(schema.properties)} properties"
        )

    keys = {s.key for _, s in seen}
    if len(keys) > 1:
        note("  The key property name CHANGES with language on this farm. Pin it with")
        note("  --language so two machines cannot disagree about the schema.")
    elif seen:
        note("  Language did not change the key here — MUI is off, or no language pack")
        note("  is installed for the alternatives, so --language buys nothing.")


# --------------------------------------------------------------------------- #
# paging
# --------------------------------------------------------------------------- #


def parse_feed(body: bytes) -> tuple[list[dict[str, Any]], str | None]:
    """Rows and the continuation link, from either representation."""
    text = body.decode("utf-8", "replace").strip()
    if text.startswith("{"):
        payload = json.loads(text)
        node = payload.get("d") if isinstance(payload, dict) else None
        if isinstance(node, dict):
            rows = node.get("results")
            if isinstance(rows, list):
                return [r for r in rows if isinstance(r, dict)], node.get("__next")
            return [node], None
        if isinstance(node, list):
            return [r for r in node if isinstance(r, dict)], None
        return [], None
    rows, next_url = parse_atom_feed(etree.fromstring(body))
    return [r for r in rows if isinstance(r, dict)], next_url


class PageUnavailable(ODataError):
    """A page did not come back. ``status`` is 0 when the request never
    completed at all — a read timeout, a connection failure, or 5xx that
    outlasted the retries — as opposed to a status the server chose to send."""

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


#: Halving stops here. Below this a page is small enough that the size is not
#: what is wrong, and continuing to shrink only delays an honest failure.
MIN_PAGE_SIZE = 25


def fetch_page(transport: Transport, url: str) -> tuple[list[dict[str, Any]], str | None]:
    status, detail, body = probe(transport, url)
    if not (200 <= status < 300):
        raise PageUnavailable(f"HTTP {status} for {url}: {detail}", status=status)
    return parse_feed(body)


class SchemaMismatch(ODataError):
    """Rows already on disk were written under different property names."""


def first_row_keys(path: Path) -> set[str]:
    """Property names on the first usable row already on disk, if any."""
    if not path.exists():
        return set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                return set(row)
    return set()


def highest_id_on_disk(path: Path, key: str) -> int:
    """Resume point. Rows are written in key order, but scan rather than trust it.

    A run killed mid-write can leave a truncated final line; a scan that skips
    unparseable lines resumes correctly where reading only the last line would
    either crash or silently restart from zero.
    """
    if not path.exists():
        return 0
    best = 0
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row_id = json.loads(line).get(key)
            except json.JSONDecodeError:
                continue
            if isinstance(row_id, int) and row_id > best:
                best = row_id
    return best


def page_url(base: str, *, key: str | None, last_id: int, page_size: int, expand: Sequence[str]) -> str:
    """One page's query. Key paging when a key exists, plus any expansions."""
    options: list[str] = []
    if key:
        options += [f"$filter={key}%20gt%20{last_id}", f"$orderby={key}", f"$top={page_size}"]
    if expand:
        options.append("$expand=" + ",".join(expand))
    return f"{base}?{'&'.join(options)}" if options else base


def max_key(rows: Sequence[dict[str, Any]], key: str) -> int | None:
    values = [r[key] for r in rows if isinstance(r.get(key), int)]
    return max(values) if values else None


def pull(
    transport: Transport,
    service: ODataService,
    schema: CollectionSchema,
    path: Path,
    *,
    page_size: int,
    limit: int | None,
    skip_count: bool = False,
    expand: Sequence[str] | None = None,
) -> int:
    """Page one collection to JSONL, resuming from whatever is already there.

    Two strategies, in order of preference:

    **Key paging** — ``$filter=<key> gt N&$orderby=<key>``. Resumable across
    runs, because the checkpoint is a value in the data rather than a token the
    server minted.

    **Server-driven paging** — take the default page and follow ``__next``.
    Needs no property names at all, so it survives a service that will not let
    its key be filtered. Not resumable across runs: the continuation is the
    server's, and it is not offered again on a later connection. Rows already
    on disk are skipped by key where a key is known, so a rerun costs requests
    but never duplicates.
    """
    entity_set = schema.entity_set
    path.parent.mkdir(parents=True, exist_ok=True)
    base = f"{service.endpoint}/{entity_set}"
    key = schema.key
    last_id = highest_id_on_disk(path, key) if key else 0

    if skip_count:
        expected = "not counted (--no-count)"
    else:
        try:
            expected = f"{service.count(entity_set):,}"
        except (ODataError, RetryableTransportError):
            # A count over the view threshold throws where a paged read does not.
            # Not knowing the total is a cosmetic loss; refusing to run over it
            # would be a real one.
            #
            # RetryableTransportError belongs here as much as ODataError: this
            # farm answers a count on the 100k comment list with HTTP 500, which
            # is retryable, so it arrives as a transport failure rather than an
            # OData one. Catching only ODataError let a cosmetic step abort the
            # whole extraction.
            expected = "unknown (count refused)"

    # Switching --language renames the key, and a resume scan looking for the new
    # name in a file written under the old one finds nothing, restarts at zero and
    # appends the whole list a second time. Refuse instead of duplicating silently.
    if key:
        existing = first_row_keys(path)
        if existing and key not in existing:
            raise SchemaMismatch(
                f"{path} holds rows without a {key!r} property "
                f"(they have: {', '.join(sorted(existing)[:8])}). "
                "The language changed between runs, so resuming would append every "
                "row again. Delete that file, or pass a different --out."
            )

    note(f"  {entity_set}: {expected} row(s) expected, {schema.describe()}")
    if last_id:
        note(f"  resuming after {key} {last_id:,}")

    expansions = list(expand or [])
    if expansions:
        note(f"  expanding: {', '.join(expansions)}")

    url: str | None = None
    if key:
        url = page_url(base, key=key, last_id=last_id, page_size=page_size, expand=expansions)
    else:
        note("  no key property on this collection — using server-driven paging")
        url = page_url(base, key=None, last_id=0, page_size=page_size, expand=expansions)

    written = skipped = pages = 0
    started = time.monotonic()
    seen_on_disk = last_id

    with path.open("a", encoding="utf-8") as handle:
        while True:
            if url is None:
                url = base  # server's default page, no options
            try:
                rows, continuation = fetch_page(transport, url)
            except ODataError as exc:
                # A request that never completed is a request that asked for too
                # much. Halving the page attacks the actual cause; dropping
                # $expand or the key filter would not, and this can strike on any
                # page -- the farm answered four 500-row pages before the fifth
                # exceeded its read timeout.
                if getattr(exc, "status", None) == 0 and key and page_size // 2 >= MIN_PAGE_SIZE:
                    page_size //= 2
                    note(f"  ! page {pages + 1} did not complete: {exc}")
                    note(f"    retrying the same range at $top={page_size}")
                    url = page_url(base, key=key, last_id=last_id, page_size=page_size, expand=expansions)
                    continue
                if pages == 0 and expansions:
                    # $expand is optional data. Losing it costs some payload;
                    # losing the run costs the extraction.
                    note(f"  ! $expand({', '.join(expansions)}) was refused: {exc}")
                    note("    retrying without it — the stub fields stay unresolved")
                    expansions = []
                    url = page_url(base, key=key, last_id=last_id, page_size=page_size, expand=[])
                    continue
                if pages == 0 and key:
                    # The key is named right for this service and still refused.
                    # Server-driven paging asks for nothing by name, so try it
                    # before giving up.
                    note(f"  ! key paging on {key!r} was refused: {exc}")
                    note("    falling back to server-driven paging (follow __next)")
                    key = None
                    url = page_url(base, key=None, last_id=0, page_size=page_size, expand=expansions)
                    continue
                note(f"  ! failed on page {pages + 1} ({written:,} rows written, $top={page_size})")
                note(f"    retry this query with: --diagnose --page-size {page_size}")
                raise
            if not rows:
                break

            fresh = rows
            if key is None and seen_on_disk and schema.key:
                # Continuation mode restarts at the top of the list; drop what a
                # previous run already wrote rather than duplicating it.
                fresh = [r for r in rows if not _at_or_below(r, schema.key, seen_on_disk)]
                skipped += len(rows) - len(fresh)

            for row in fresh:
                handle.write(json.dumps(row, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()
            written += len(fresh)
            pages += 1

            rate = written / max(time.monotonic() - started, 1e-6)
            note(f"    page {pages}: +{len(fresh)} → {written:,} rows ({rate:.0f}/s)")

            if limit is not None and written >= limit:
                note(f"  stopping at --limit {limit}")
                break

            if key:
                highest = max_key(rows, key)
                if highest is None or highest <= last_id:
                    # Without a strictly advancing key the next request would
                    # repeat this one forever. Stop and say so rather than spin.
                    note(f"  ! page {pages} did not advance past {key} {last_id} — stopping")
                    break
                last_id = highest
                url = page_url(base, key=key, last_id=last_id, page_size=page_size, expand=expansions)
            else:
                if not continuation:
                    break
                url = continuation

    if skipped:
        note(f"  {skipped:,} row(s) already on disk were skipped")
    note(f"  {entity_set}: {written:,} new row(s) → {path}")
    return written


def _at_or_below(row: dict[str, Any], key: str, ceiling: int) -> bool:
    value = row.get(key)
    return isinstance(value, int) and value <= ceiling


DEFAULT_USERS = "UserInformationList"


def pull_users(
    transport: Transport,
    service: ODataService,
    available: list[str],
    path: Path,
    *,
    page_size: int,
) -> int | None:
    """Put names to the numeric author ids. Returns ``None`` if the list is absent.

    Every comment carries ``CreatedById`` and nothing else about its author, and
    §6.3 of the pipeline specification classifies a conversation partly by who
    wrote each turn. The corpus has 59 distinct comment authors across 102,625
    comments, so resolving them through ``$expand`` would be a hundred thousand
    lookups to learn fifty-nine facts. This list answers it in one pass.
    """
    entity_set = resolve_optional_entity_set(service, DEFAULT_USERS, available)
    if entity_set is None:
        note(f"  {DEFAULT_USERS} is not exposed on this web — author ids stay unresolved")
        return None

    schema = discover_schema(transport, service, entity_set)
    note(f"  {entity_set}: {schema.describe()}")
    return pull(transport, service, schema, path, page_size=page_size, limit=None, skip_count=True)


# --------------------------------------------------------------------------- #
# attachments
# --------------------------------------------------------------------------- #

DEFAULT_ATTACHMENTS = "Attachments"

#: Extension groups worth telling apart. An archive is the interesting case: a
#: zip of log files is a directory of evidence, not one opaque blob.
FILE_KINDS: dict[str, tuple[str, ...]] = {
    "archive": (".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".cab"),
    "document": (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".rtf", ".odt", ".msg", ".eml"),
    "image": (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff", ".heic"),
    "text": (".txt", ".log", ".csv", ".xml", ".json", ".ini", ".cfg", ".yml", ".yaml"),
    "video": (".mp4", ".avi", ".mov", ".mkv", ".wmv"),
}


def file_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    for kind, suffixes in FILE_KINDS.items():
        if suffix in suffixes:
            return kind
    return "other"


def attachment_urls(web_url: str, entity_set: str, item_id: int, name: str) -> tuple[str, str]:
    """The two ways SharePoint 2010 will hand over an attachment's bytes.

    ``AttachmentsItem`` is a media link entry — ``HasStream`` is true — so the
    entity carries only (EntitySet, ItemId, Name) and the content lives at a
    separate media resource. Which of the two forms a given build serves is not
    something to assume, so both are constructed and both get tried.

    Two encoding traps, and neither produces a clean 404 when got wrong:

    * A single quote terminates an OData string literal. ``O'Brien.pdf`` has to
      become ``'O''Brien.pdf'`` or the key is malformed.
    * Spaces and umlauts must be percent-encoded in both forms.
    """
    # The apostrophe stays literal. It is a legal path character, and doubling is
    # how OData escapes it inside a string literal — percent-encoding the doubled
    # pair as well leaves the service to decode and then un-double, which key
    # parsers do not reliably do.
    literal = quote(name, safe="'").replace("'", "''")
    key = f"EntitySet='{quote(entity_set, safe='')}',ItemId={int(item_id)},Name='{literal}'"
    media = f"{web_url}/_vti_bin/ListData.svc/{DEFAULT_ATTACHMENTS}({key})/$value"
    direct = f"{web_url}/Lists/{quote(entity_set, safe='')}/Attachments/{int(item_id)}/{quote(name, safe='')}"
    return media, direct


#: Enough of a file to identify it without pulling a 200 MB video across the wire.
PROBE_BYTES = 2 * 1024 * 1024


def fetch_attachment(
    transport: Transport,
    web_url: str,
    entity_set: str,
    item_id: int,
    name: str,
    *,
    save_to: Path | None,
) -> dict[str, Any]:
    """Try to actually retrieve one attachment. Reports rather than raises.

    Both URL forms are attempted because which one a build serves is not
    knowable in advance, and a failure of one is not a failure of the file.
    Reading is capped at :data:`PROBE_BYTES`: the point is to prove the bytes are
    reachable and see what they are, not to move the corpus.
    """
    media, direct = attachment_urls(web_url, entity_set, item_id, name)
    result: dict[str, Any] = {
        "item_id": item_id,
        "name": name,
        "kind": file_kind(name),
        "ok": False,
        "via": None,
        "bytes": 0,
        "content_type": None,
        "error": None,
    }
    for label, url in (("media", media), ("direct", direct)):
        try:
            response = transport.request("GET", url, stream=True)
        except TransportError as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code >= 400:
            result["error"] = f"HTTP {response.status_code}"
            continue
        payload = response.content[:PROBE_BYTES]
        result.update(
            ok=True,
            via=label,
            bytes=len(payload),
            content_type=response.headers.get("Content-Type"),
            error=None,
        )
        if save_to is not None:
            save_to.mkdir(parents=True, exist_ok=True)
            # Attachment names repeat across tickets, so the item id has to be in
            # the filename or one ticket's evidence silently overwrites another's.
            safe = re.sub(r"[^\w.\- ]", "_", name).strip() or "attachment"
            (save_to / f"{item_id}__{safe}").write_bytes(payload)
        break
    return result


def survey_attachments(
    transport: Transport,
    service: ODataService,
    available: list[str],
    out_dir: Path,
    *,
    page_size: int,
    sample: int,
) -> None:
    """Inventory every attachment, then prove a sample of them can be fetched.

    Deliberately not a download pipeline. The question is whether the bytes are
    reachable at all and what they are — a zip of log files is worth a different
    plan from a folder of screenshots.
    """
    entity_set = resolve_optional_entity_set(service, DEFAULT_ATTACHMENTS, available)
    if entity_set is None:
        note(f"  {DEFAULT_ATTACHMENTS} is not exposed on this web")
        return

    path = out_dir / "attachments.jsonl"
    schema = discover_schema(transport, service, entity_set)
    note(f"  {entity_set}: {schema.describe()}")
    # The key is (EntitySet, ItemId, Name) — there is no integer Id to page on,
    # so this collection walks by continuation rather than by key.
    pull(transport, service, schema, path, page_size=page_size, limit=None, skip_count=True)

    rows = read_jsonl(path)
    if not rows:
        note("  no attachments on this web")
        return

    kinds: Counter[str] = Counter()
    per_list: Counter[str] = Counter()
    for r in rows:
        kinds[file_kind(str(r.get("Name", "")))] += 1
        per_list[str(r.get("EntitySet", "?"))] += 1
    note(f"\n  {len(rows):,} attachment(s)")
    note("  by list:  " + ", ".join(f"{k}={v:,}" for k, v in per_list.most_common()))
    note("  by kind:  " + ", ".join(f"{k}={v:,}" for k, v in kinds.most_common()))
    tickets_with = len({r.get("ItemId") for r in rows if r.get("EntitySet") == "Ticket"})
    note(f"  tickets carrying at least one: {tickets_with:,}")

    # One of each kind, so the check covers the formats rather than the first N
    # rows, which on a sorted list would all be the same thing.
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_kind[file_kind(str(r.get("Name", "")))].append(r)
    picked: list[dict[str, Any]] = []
    while len(picked) < sample and any(by_kind.values()):
        for kind in list(by_kind):
            if by_kind[kind] and len(picked) < sample:
                picked.append(by_kind[kind].pop(0))

    save_to = out_dir / "attachments_sample"
    note(f"\n  fetching {len(picked)} of them into {save_to.name}/")
    ok = 0
    for r in picked:
        item_id = r.get("ItemId")
        name = str(r.get("Name", ""))
        if not isinstance(item_id, int):
            continue
        outcome = fetch_attachment(
            transport,
            service.web_url,
            str(r.get("EntitySet", "Ticket")),
            item_id,
            name,
            save_to=save_to,
        )
        ok += 1 if outcome["ok"] else 0
        status = f"ok via {outcome['via']}" if outcome["ok"] else f"FAILED {outcome['error']}"
        note(
            f"    [{outcome['kind']:<8}] {name[:44]:<44} {status} "
            f"{format_bytes(outcome['bytes']) if outcome['ok'] else ''}"
        )
    note(
        f"\n  {ok}/{len(picked)} retrieved. "
        + (
            "Attachment bytes are reachable over REST."
            if ok
            else "No form worked — the bytes are not reachable this way."
        )
    )


# --------------------------------------------------------------------------- #
# joining
# --------------------------------------------------------------------------- #


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def parent_value(row: dict[str, Any], key: str) -> int | None:
    """The parent ticket Id, whether the field came back flat or as a lookup."""
    value = row.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    if isinstance(value, dict):
        inner = resolve_property(value, KEY_ALIASES)
        target = value.get(inner) if inner else None
        return target if isinstance(target, int) else None
    return None


def candidate_parent_keys(row: dict[str, Any]) -> list[str]:
    """Properties that could hold a foreign key to another list.

    ``ListData.svc`` renders a Lookup column as a navigation property plus a
    scalar ``<Name>Id`` holding the target row's key. That scalar is named after
    the lookup's *display column*, not the list it points at — here a comment's
    link to its ticket is ``TicketNumberId``.

    Excluded are the key itself and the housekeeping foreign keys every list
    carries. Those are matched by role, not by their English spelling: on a
    German web they arrive as ``ErstelltVonId`` and ``GeändertVonId``, and an
    English-only exclusion list would offer them as parent candidates.
    """
    ignored = {row_key for row_key in row if resolve_property([row_key], KEY_ALIASES) == row_key}
    for alias in HOUSEKEEPING_FK_ALIASES:
        found = resolve_property(row, [alias])
        if found:
            ignored.add(found)
    ignored.update(n for n in row if n.lower().startswith("owshiddenversion"))
    return [
        key for key in row if key not in ignored and (key.endswith(("Id", "ID")) or "parent" in key.lower())
    ]


def detect_parent_key(rows: list[dict[str, Any]], ticket_ids: set[int] | None = None) -> str | None:
    """Which property points a comment at its ticket.

    Guessing from names does not survive contact with a real farm — there is no
    ``ParentID`` here, and the column that does the job is named after a lookup's
    display column. So when the ticket Ids are known, score each candidate by how
    many of its values actually *are* ticket Ids and let the data decide. That
    identifies the relationship regardless of what anyone called it.

    Without ticket Ids to check against, fall back to counting non-empty values,
    which is weaker but still better than a name match.
    """
    sample = rows[:500]
    if not sample:
        return None

    scored: list[tuple[int, int, str]] = []
    for key in candidate_parent_keys(sample[0]):
        values = [v for v in (parent_value(row, key) for row in sample) if v is not None]
        if not values:
            continue
        hits = sum(1 for v in values if v in ticket_ids) if ticket_ids else len(values)
        if hits:
            # Ties break toward the column that is populated more often.
            scored.append((hits, len(values), key))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


def sort_key(row: dict[str, Any]) -> tuple[float, int]:
    """Order a thread by when each comment was written.

    ``Edm.DateTime`` reaches us as a *string* in both representations and is
    never decoded on this path — ``parse_odata_datetime`` runs only inside
    ``ODataRowMapper``, which the landing-zone crawl uses and this script does
    not. So the two serialisations arrive in different formats:

    * JSON  ``/Date(1748528100000)/``
    * Atom  ``2026-05-29T14:35:00``

    Sorting those as text happens to work for ISO, and happens to work for
    ``/Date(ms)/`` only while every timestamp has the same digit count — true
    between 2001-09-09 and 2286, false for anything older, and false again for
    the ``/Date(ms+0060)/`` offset form. Decoding to an instant removes the
    coincidence. ``Created`` is nullable in the schema, so undated comments
    sort first rather than crashing the sort.
    """
    created_name = resolve_property(row, CREATED_ALIASES)
    created = row.get(created_name) if created_name else None
    moment = parse_odata_datetime(created) if isinstance(created, str) else None
    key_name = resolve_property(row, KEY_ALIASES)
    row_id = row.get(key_name) if key_name else None
    return (moment.timestamp() if moment else float("-inf"), row_id if isinstance(row_id, int) else 0)


def join(tickets_path: Path, comments_path: Path, out_path: Path, parent_key: str | None) -> None:
    """Emit one line per ticket, with its conversation attached in time order."""
    comments = read_jsonl(comments_path)
    if not comments:
        note("  nothing to join — no comments on disk")
        return

    tickets = read_jsonl(tickets_path)
    ticket_key = resolve_property(tickets[0], KEY_ALIASES) if tickets else None
    ticket_ids = (
        {t[ticket_key] for t in tickets if isinstance(t.get(ticket_key), int)} if ticket_key else set()
    )

    key = parent_key or detect_parent_key(comments, ticket_ids)
    if key is None:
        note("  ! could not identify the parent field on the comment rows.")
        note(f"    Foreign-key candidates: {', '.join(candidate_parent_keys(comments[0])) or 'none'}")
        note(f"    All properties: {', '.join(sorted(comments[0]))}")
        note("    Rerun with --parent-key <name> once you spot it.")
        return
    note(
        f"  joining on {key!r}" + (f" (matched against {len(ticket_ids):,} ticket Ids)" if ticket_ids else "")
    )

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    orphans = 0
    for comment in comments:
        parent = parent_value(comment, key)
        if parent is None:
            orphans += 1
            continue
        grouped[parent].append(comment)

    for thread in grouped.values():
        thread.sort(key=sort_key)

    matched = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for ticket in tickets:
            ticket_id = ticket.get(ticket_key) if ticket_key else None
            thread = grouped.get(ticket_id, []) if isinstance(ticket_id, int) else []
            if thread:
                matched += 1
            handle.write(json.dumps({**ticket, "comments": thread}, ensure_ascii=False, default=str))
            handle.write("\n")

    note(f"  {matched:,}/{len(tickets):,} ticket(s) have at least one comment → {out_path}")
    if orphans:
        note(f"  {orphans:,} comment(s) had no usable {key} and were left out of the join")
    unmatched = set(grouped) - ticket_ids
    if unmatched:
        note(
            f"  {len(unmatched):,} parent Id(s) in comments match no ticket on disk "
            "— expected if the ticket pull is incomplete or --limit was used"
        )


# --------------------------------------------------------------------------- #
# narration
# --------------------------------------------------------------------------- #

RELEVANT_SETTINGS = (
    "base_url",
    "auth_mode",
    "username",
    "verify_ssl",
    "allow_legacy_tls",
    "ntlm_send_cbt",
    "ntlm_prime_connection",
    "requests_per_second",
    "timeout_seconds",
    "max_retries",
)


def banner(settings: Settings, out: Path, language: str | None = None) -> None:
    """Everything that decides whether a request succeeds, before one is sent.

    A failure report that omits the effective configuration is a failure report
    that cannot be acted on — half the ways this can break are settings the
    operator believes are set differently than they are.

    Printed before the transport is built, so the warnings that construction
    emits (legacy TLS, unverified certificates, a bare-IP base URL) appear
    underneath the configuration that caused them rather than above it.
    """
    redacted = settings.redacted_dict()
    width = max(len(k) for k in RELEVANT_SETTINGS)
    note("=" * 72)
    note("spconnect — tickets + comments over ListData.svc")
    note("=" * 72)
    note(f"  {'endpoint':<{width}} {settings.base_url}/_vti_bin/ListData.svc")
    note(f"  {'output':<{width}} {out.resolve()}")
    for key in RELEVANT_SETTINGS:
        note(f"  {key:<{width}} {redacted.get(key)}")
    negotiated = language or "(unset — the web's own language)"
    note(f"  {'Accept-Language':<{width}} {negotiated}")
    if settings.log_bodies:
        note(f"  {'body trace':<{width}} {settings.resolved_trace_file}")
    note("=" * 72)


def footer(transport: Transport, started: float) -> None:
    total = transport.request_count + transport.side_channel_requests
    elapsed = time.monotonic() - started
    note("")
    note("-" * 72)
    note(
        f"  {total} HTTP request(s), {format_bytes(transport.bytes_received)} received, "
        f"{elapsed:.1f}s elapsed"
    )
    note("-" * 72)


def explain_failure(exc: BaseException) -> list[str]:
    """What this specific exception means here, and what to run next.

    Keyed on type rather than on message text: the messages come from three
    different layers and are not stable enough to match on.
    """
    if isinstance(exc, AuthenticationError):
        return [
            "The credential was refused (401/403).",
            "`spconnect probe-rest` diagnoses this properly — it compares a GET",
            "against a POST on the same directory and names which one broke.",
        ]
    if isinstance(exc, RedirectRefused):
        return [
            "The server redirected rather than answering. That is usually a sign-in",
            "page, an alternate access mapping, or the wrong zone for SP_BASE_URL.",
            f"Suggested base URL, if any: {getattr(exc, 'suggested_base_url', None)}",
        ]
    if isinstance(exc, ODataUnavailable | NotFoundError):
        return [
            "ListData.svc answered 404 — the OData feature is off on this web, or",
            "SP_BASE_URL points at a web that does not have it.",
            "Confirm with: spconnect probe-rest",
        ]
    if isinstance(exc, ODataNotJson):
        return [
            "The service answered in a representation this build does not render as",
            "JSON. That is expected for the service document and handled; seeing it",
            "on a feed is not. Rerun with -v and send the http.response line.",
        ]
    if isinstance(exc, RetryableTransportError):
        return [
            "The request never completed — connection, timeout, or a 5xx that",
            "survived all retries. If this is TLS, SP_ALLOW_LEGACY_TLS=true and",
            "SP_VERIFY_SSL=false are the two settings that matter.",
            "Rerun with -v to see how far the retries got.",
        ]
    if isinstance(exc, SchemaMismatch):
        return [
            "Nothing was written. The rows already in that file use different",
            "property names than this run resolved, which happens when --language",
            "changes between runs. Delete the file or use a different --out;",
            "appending would have duplicated every row.",
        ]
    if isinstance(exc, ODataError):
        return [
            "ListData.svc returned an error body. On a list over 5000 items this is",
            "usually the list view threshold — but a read paged on Id should not hit",
            "it, so the URL in the message is the thing to look at.",
            "Rerun with -v --log-bodies and send the response.",
        ]
    return [
        "Unexpected failure — the traceback above is the evidence.",
        "Rerun with -v --log-bodies for the full request/response record.",
    ]


# --------------------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add = parser.add_argument
    add("--out", type=Path, default=Path("./landing-rest"), help="output directory")
    add("--tickets", default=DEFAULT_TICKETS, help=f"ticket collection (default {DEFAULT_TICKETS})")
    add("--comments", default=DEFAULT_COMMENTS, help=f"comment collection (default {DEFAULT_COMMENTS})")
    add("--page-size", type=int, default=500, help="rows per request (server caps at 1000)")
    add("--limit", type=int, default=None, help="stop after N new rows per collection")
    add("--parent-key", default=None, help="comment property naming the ticket, if autodetect fails")
    add("--list-sets", action="store_true", help="print every collection and exit")
    add("--diagnose", action="store_true", help="bisect the query options against the ticket list and exit")
    add("--no-join", action="store_true", help="skip the joined output file")
    add(
        "--expand-comments",
        default=None,
        help="expand the comment list too. Rarely wanted: it is what timed the last run out.",
    )
    add("--no-users", action="store_true", help="skip the user list pull")
    add(
        "--attachments",
        action="store_true",
        help="inventory attachments and verify a sample can be fetched, then exit",
    )
    add(
        "--attachment-sample",
        type=int,
        default=8,
        help="how many attachments to actually retrieve (default 8)",
    )
    add(
        "--expand",
        default=None,
        help="tickets only: 'auto' for every navigation stub, or a comma list. Default: none.",
    )
    add(
        "--no-count",
        action="store_true",
        help="skip the row count; it costs retries when the server refuses it",
    )
    add("--language", default=None, help="Accept-Language for every request, e.g. en-US (default: unset)")
    add("--env-file", default=None, help="path to .env")
    add("-v", "--verbose", action="store_true", help="log every HTTP request and response")
    add("--log-bodies", action="store_true", help="also write raw bodies to a trace file")
    add("--trace-file", default=None, help="where --log-bodies writes (default <landing>/_trace.log)")
    args = parser.parse_args()

    overrides: dict[str, Any] = {}
    if args.log_bodies:
        overrides["log_bodies"] = True
    if args.trace_file:
        overrides["trace_file"] = args.trace_file

    settings = load_settings(env_file=args.env_file, overrides=overrides)
    setup_logging("DEBUG" if args.verbose else "INFO", settings.log_format)

    started = time.monotonic()
    banner(settings, args.out, args.language)
    transport = Transport(settings)
    if args.language:
        # On the session, not per request: ODataService builds its own requests
        # for the service document and the count, and those must ask in the same
        # language as everything else or the schema will not be consistent.
        transport.session.headers["Accept-Language"] = args.language
    service = ODataService(transport, settings.base_url)

    try:
        # --list-sets and --diagnose both stop after step 2. Labelling those runs
        # "2/4" reads as a failure two steps into four, which is how the last one
        # got reported.
        steps = 2 if (args.list_sets or args.diagnose or args.attachments) else 4
        note(f"\n[1/{steps}] reading the service document")
        available = service.entity_sets()
        note(f"      {len(available)} collection(s), served as {service.representation or 'atom'}")

        if args.list_sets:
            note("")
            for name in sorted(available):
                print(name)
            footer(transport, started)
            return 0

        if args.attachments:
            note("\nattachments")
            survey_attachments(
                transport,
                service,
                available,
                args.out,
                page_size=args.page_size,
                sample=args.attachment_sample,
            )
            footer(transport, started)
            return 0

        note(f"\n[2/{steps}] resolving collection names")
        tickets_set = resolve_entity_set(service, args.tickets, available)
        comments_set = resolve_entity_set(service, args.comments, available)
        note(f"      tickets  -> {tickets_set}")
        note(f"      comments -> {comments_set}")

        if args.diagnose:
            # Both collections, not just tickets. The comment list is the larger
            # of the two by an order of magnitude, so it is the one where a view
            # threshold or an unqueryable column would actually bite, and it was
            # going unprobed.
            for entity_set in (tickets_set, comments_set):
                diagnose(transport, service, entity_set, args.page_size)
            compare_languages(transport, service, tickets_set)
            note("")
            footer(transport, started)
            return 0

        # One row from each collection, to learn what this service calls its key
        # and its Created column. Both are localised, so neither can be assumed.
        note("      reading one row from each to learn the property names")
        ticket_schema = discover_schema(transport, service, tickets_set)
        comment_schema = discover_schema(transport, service, comments_set)
        note(f"      tickets  {ticket_schema.describe()}")
        note(f"      comments {comment_schema.describe()}")
        for schema in (ticket_schema, comment_schema):
            if schema.key is None:
                note(f"      ! no key property found on {schema.entity_set} — server-driven paging only")
            if schema.deferred:
                note(f"      {schema.entity_set} stubs: {', '.join(schema.deferred)}")

        plan = expansion_plan(
            ticket_schema,
            comment_schema,
            tickets_spec=args.expand,
            comments_spec=args.expand_comments,
        )
        for name in plan.unknown:
            note(f"      ! --expand {name!r}: no such navigation property, ignored")

        tickets_path = args.out / "tickets.jsonl"
        comments_path = args.out / "comments.jsonl"

        note(f"\n[3/{steps}] pulling {tickets_set}")
        pull(
            transport,
            service,
            ticket_schema,
            tickets_path,
            page_size=args.page_size,
            limit=args.limit,
            skip_count=args.no_count,
            expand=plan.tickets,
        )
        note(f"\n[4/{steps}] pulling {comments_set}")
        pull(
            transport,
            service,
            comment_schema,
            comments_path,
            page_size=args.page_size,
            limit=args.limit,
            skip_count=args.no_count,
            expand=plan.comments,
        )

        if not args.no_users:
            note("\nresolving author ids")
            pull_users(transport, service, available, args.out / "users.jsonl", page_size=args.page_size)

        if not args.no_join:
            note("\njoining")
            join(tickets_path, comments_path, args.out / "tickets_with_comments.jsonl", args.parent_key)

    except KeyboardInterrupt:
        note("\n\ninterrupted — rerun to resume from the last Id written")
        footer(transport, started)
        return 130
    except (TransportError, ODataError) as exc:
        note(f"\n\nFAILED: {type(exc).__name__}: {exc}")
        if args.verbose:
            note("\n" + traceback.format_exc())
        note("")
        for line in explain_failure(exc):
            note(f"  {line}")
        footer(transport, started)
        return 2 if isinstance(exc, AuthenticationError) else 1
    except Exception as exc:
        # Anything not from our own layers is a bug or an environment problem,
        # and the traceback is the only useful thing to say about it. Print it
        # unconditionally rather than hiding it behind -v.
        note(f"\n\nFAILED: {type(exc).__name__}: {exc}\n")
        note(traceback.format_exc())
        for line in explain_failure(exc):
            note(f"  {line}")
        footer(transport, started)
        return 1
    finally:
        transport.close()

    footer(transport, started)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
