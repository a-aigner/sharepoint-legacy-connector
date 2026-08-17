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
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from spconnect.config import load_settings, setup_logging
from spconnect.services.odata import ODataError, ODataService, normalise_name
from spconnect.transport import AuthenticationError, Transport

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
            page = service.get_items(entity_set, last_id=last_id, top=page_size)
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


def detect_parent_key(rows: list[dict[str, Any]]) -> str | None:
    """Which property points a comment at its ticket.

    The display page links a new comment with ``?ParentID=14819``, but that is
    the query string, not necessarily the OData property — a lookup surfaces as
    ``<Name>Id`` while a plain number field keeps its own name. So score the
    candidates on real data instead of assuming either.
    """
    scored: list[tuple[int, str]] = []
    sample = rows[:200]
    for key in sample[0] if sample else []:
        if "parent" not in key.lower():
            continue
        hits = sum(1 for row in sample if parent_value(row, key) is not None)
        if hits:
            scored.append((hits, key))
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


def sort_key(row: dict[str, Any]) -> tuple[str, int]:
    created = row.get("Created")
    return (str(created) if created is not None else "", row.get("Id") or 0)


def join(tickets_path: Path, comments_path: Path, out_path: Path, parent_key: str | None) -> None:
    """Emit one line per ticket, with its conversation attached in time order."""
    comments = read_jsonl(comments_path)
    if not comments:
        note("  nothing to join — no comments on disk")
        return

    key = parent_key or detect_parent_key(comments)
    if key is None:
        note(
            "  ! could not identify the parent field on the comment rows.\n"
            f"    Properties present: {', '.join(sorted(comments[0]))}\n"
            "    Rerun with --parent-key <name> once you spot it."
        )
        return
    note(f"  joining on {key!r}")

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

    tickets = read_jsonl(tickets_path)
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
    add("--no-join", action="store_true", help="skip the joined output file")
    add("--env-file", default=None, help="path to .env")
    add("-v", "--verbose", action="store_true", help="debug logging, including every URL")
    args = parser.parse_args()

    settings = load_settings(env_file=args.env_file)
    setup_logging("DEBUG" if args.verbose else "WARNING", settings.log_format)

    transport = Transport(settings)
    service = ODataService(transport, settings.base_url)

    try:
        note(f"ListData.svc → {service.endpoint}")
        available = service.entity_sets()
        note(f"{len(available)} collection(s) visible, served as {service.representation or 'atom'}\n")

        if args.list_sets:
            for name in sorted(available):
                print(name)
            return 0

        tickets_set = resolve_entity_set(service, args.tickets, available)
        comments_set = resolve_entity_set(service, args.comments, available)

        tickets_path = args.out / "tickets.jsonl"
        comments_path = args.out / "comments.jsonl"

        note(f"tickets  <- {tickets_set}")
        pull(service, tickets_set, tickets_path, page_size=args.page_size, limit=args.limit)
        note(f"\ncomments <- {comments_set}")
        pull(service, comments_set, comments_path, page_size=args.page_size, limit=args.limit)

        if not args.no_join:
            note("\njoining")
            join(tickets_path, comments_path, args.out / "tickets_with_comments.jsonl", args.parent_key)

    except AuthenticationError as exc:
        note(f"\nAUTH FAILED: {exc}\n\nRun `spconnect probe-rest` — it explains this one properly.")
        return 2
    except ODataError as exc:
        note(f"\nREST FAILED: {exc}")
        return 1
    except KeyboardInterrupt:
        note("\ninterrupted — rerun to resume from the last Id written")
        return 130
    finally:
        transport.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
