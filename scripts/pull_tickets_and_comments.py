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
import sys
import time
import traceback
from collections import defaultdict
from pathlib import Path
from typing import Any

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


def probe(transport: Transport, url: str) -> tuple[int, str, bytes]:
    """One GET that reports rather than raises. Returns ``(status, detail, body)``."""
    try:
        response = transport.request("GET", url, headers={"Accept": ACCEPT_ENTITIES})
    except AuthenticationError:
        return 401, "credential refused", b""
    except NotFoundError:
        return 404, "not found", b""
    except TransportError as exc:
        return 0, f"{type(exc).__name__}: {exc}", b""
    if response.status_code >= 400:
        return response.status_code, error_message(response.content), response.content
    return response.status_code, f"{len(response.content):,} bytes", response.content


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
        ("$inlinecount", "?$top=0&$inlinecount=allpages"),
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
# paging
# --------------------------------------------------------------------------- #


def highest_id_on_disk(path: Path) -> int:
    """Resume point. Rows are written in Id order, but scan rather than trust it.

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
                row_id = json.loads(line).get("Id")
            except json.JSONDecodeError:
                continue
            if isinstance(row_id, int) and row_id > best:
                best = row_id
    return best


def pull(
    service: ODataService,
    entity_set: str,
    path: Path,
    *,
    page_size: int,
    limit: int | None,
) -> int:
    """Page one collection to JSONL, resuming from whatever is already there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    last_id = highest_id_on_disk(path)

    try:
        total = service.count(entity_set)
        expected = f"{total:,}"
    except ODataError:
        # A count over the view threshold throws where a paged read does not.
        # Not knowing the total is a cosmetic loss; refusing to run over it
        # would be a real one.
        expected = "unknown (count refused — over the view threshold)"

    if last_id:
        note(f"  resuming {entity_set} after Id {last_id:,}")
    note(f"  {entity_set}: {expected} row(s) expected")

    written = 0
    pages = 0
    started = time.monotonic()

    with path.open("a", encoding="utf-8") as handle:
        while True:
            try:
                page = service.get_items(entity_set, last_id=last_id, top=page_size)
            except ODataError:
                # Which page died matters as much as why: a failure on the first
                # request is a rejected query, a failure on the fortieth is data
                # the server cannot render at that offset.
                note(
                    f"  ! failed on page {pages + 1} "
                    f"(after Id {last_id:,}, {written:,} rows written, $top={page_size})"
                )
                note(f"    retry just this query with: --diagnose --page-size {page_size}")
                raise
            if not page.rows:
                break

            for row in page.rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str))
                handle.write("\n")
            handle.flush()

            written += len(page.rows)
            pages += 1

            if page.max_id is None or page.max_id <= last_id:
                # Without a strictly advancing Id the next request would repeat
                # this one forever. Stop and say so rather than spin.
                note(f"  ! {entity_set}: page {pages} did not advance past Id {last_id} — stopping")
                break
            last_id = page.max_id

            rate = written / max(time.monotonic() - started, 1e-6)
            note(f"    page {pages}: +{len(page.rows)} → {written:,} rows, Id {last_id:,} ({rate:.0f}/s)")

            if limit is not None and written >= limit:
                note(f"  stopping at --limit {limit}")
                break

    note(f"  {entity_set}: {written:,} new row(s) → {path}")
    return written


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
        inner = value.get("Id")
        return inner if isinstance(inner, int) else None
    return None


#: Foreign keys every SharePoint list carries, which point at people or content
#: types rather than at a parent row.
FK_IGNORED = frozenset({"Id", "CreatedById", "ModifiedById", "ContentTypeID", "OwshiddenversionId"})


def candidate_parent_keys(row: dict[str, Any]) -> list[str]:
    """Properties that could hold a foreign key.

    ``ListData.svc`` renders a Lookup column as a navigation property plus a
    scalar ``<Name>Id`` holding the target row's ``Id``. The name of that
    scalar follows the *lookup's display column*, not the list it points at —
    on this farm a comment's link to its ticket is ``TicketNumberId``, and on
    another farm the same relationship could be called anything at all.
    """
    return [
        key
        for key in row
        if key not in FK_IGNORED and (key.endswith(("Id", "ID")) or "parent" in key.lower())
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
    created = row.get("Created")
    moment = parse_odata_datetime(created) if isinstance(created, str) else None
    row_id = row.get("Id")
    return (moment.timestamp() if moment else float("-inf"), row_id if isinstance(row_id, int) else 0)


def join(tickets_path: Path, comments_path: Path, out_path: Path, parent_key: str | None) -> None:
    """Emit one line per ticket, with its conversation attached in time order."""
    comments = read_jsonl(comments_path)
    if not comments:
        note("  nothing to join — no comments on disk")
        return

    tickets = read_jsonl(tickets_path)
    ticket_ids = {t["Id"] for t in tickets if isinstance(t.get("Id"), int)}

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
            ticket_id = ticket.get("Id")
            thread = grouped.get(ticket_id, []) if isinstance(ticket_id, int) else []
            if thread:
                matched += 1
            handle.write(json.dumps({**ticket, "comments": thread}, ensure_ascii=False, default=str))
            handle.write("\n")

    note(f"  {matched:,}/{len(tickets):,} ticket(s) have at least one comment → {out_path}")
    if orphans:
        note(f"  {orphans:,} comment(s) had no usable {key} and were left out of the join")
    unmatched = set(grouped) - {t.get("Id") for t in tickets}
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


def banner(settings: Settings, out: Path) -> None:
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
    banner(settings, args.out)
    transport = Transport(settings)
    service = ODataService(transport, settings.base_url)

    try:
        # --list-sets and --diagnose both stop after step 2. Labelling those runs
        # "2/4" reads as a failure two steps into four, which is how the last one
        # got reported.
        steps = 2 if (args.list_sets or args.diagnose) else 4
        note(f"\n[1/{steps}] reading the service document")
        available = service.entity_sets()
        note(f"      {len(available)} collection(s), served as {service.representation or 'atom'}")

        if args.list_sets:
            note("")
            for name in sorted(available):
                print(name)
            footer(transport, started)
            return 0

        note(f"\n[2/{steps}] resolving collection names")
        tickets_set = resolve_entity_set(service, args.tickets, available)
        comments_set = resolve_entity_set(service, args.comments, available)
        note(f"      tickets  -> {tickets_set}")
        note(f"      comments -> {comments_set}")

        if args.diagnose:
            diagnose(transport, service, tickets_set, args.page_size)
            footer(transport, started)
            return 0

        tickets_path = args.out / "tickets.jsonl"
        comments_path = args.out / "comments.jsonl"

        note(f"\n[3/{steps}] pulling {tickets_set}")
        pull(service, tickets_set, tickets_path, page_size=args.page_size, limit=args.limit)
        note(f"\n[4/{steps}] pulling {comments_set}")
        pull(service, comments_set, comments_path, page_size=args.page_size, limit=args.limit)

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
