# spconnect — legacy SharePoint extraction connector

Extracts every web, list, list item, field schema and file from a legacy
on-premises SharePoint farm into a local landing zone, ready for a downstream
RAG / embedding pipeline.

Targets **WSS 2.0/3.0, MOSS 2007, and SharePoint 2010** through the classic ASMX
SOAP services under `_vti_bin/`, with an optional REST backend on 2010+. There
is no CSOM, no `_api/web`, no Graph, no PnP — on the older builds those do not
exist at all, and on 2010 they do not do what this connector needs.

**Strictly read-only.** There is no code path that writes to SharePoint.

---

## Documentation

| If you are… | Read |
|---|---|
| **running the extraction** | [docs/operations.md](docs/operations.md) — install, configure, the escalation ladder |
| **stuck on an error** | [docs/troubleshooting.md](docs/troubleshooting.md) — symptom → cause → fix |
| **writing the downstream pipeline** | [docs/landing-zone.md](docs/landing-zone.md) — **the data contract** |
| **configuring it** | [docs/configuration.md](docs/configuration.md) — every setting |
| **worried about credentials** | [docs/security.md](docs/security.md) — what is protected, and what is not |
| **maintaining the code** | [docs/architecture.md](docs/architecture.md) — modules, request path, error model |
| **wondering why** | [docs/decisions.md](docs/decisions.md) — the decision record, including the wrong turns |

The original build spec is
[SPEC-legacy-sharepoint-connector.md](SPEC-legacy-sharepoint-connector.md).
Where it and these documents disagree, the documents are current —
[docs/decisions.md](docs/decisions.md) records what changed and why.

---

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ".[windows]"        # domain-joined Windows: passwordless auth

pytest                             # ~490 tests, fully offline. Do this first.

cp .env.example .env               # set SP_BASE_URL; leave auth as `integrated`
spconnect probe                    # eight narrated steps; nonzero exit on failure
```

Then follow the [escalation ladder](docs/operations.md#3-the-escalation-ladder):
`probe` → `discover` → `schema` → `graph` → `crawl --dry-run` → `verify-time` →
one list → full crawl.

---

## What it produces

```
landing/
├── _manifest.json     # what this run did, and what went wrong
├── _graph.json/.mmd   # the recovered CRM data model — look at this early
├── webs.json
└── webs/{web}/lists/{guid}/
        ├── list.json          # metadata + full field schema
        ├── items.jsonl        # one decoded item per line
        ├── items_raw.jsonl    # the same rows, exactly as the server sent them
        └── files/{item_id}/…  # attachments and document-library files
```

`doc_id` is stable across runs — upsert on it. Full contract:
[docs/landing-zone.md](docs/landing-zone.md).

---

## Commands

| Command | Purpose |
|---|---|
| `spconnect probe` | Connectivity, auth, version and capability check |
| `spconnect discover` | Webs + lists inventory, no items |
| `spconnect schema` | Field schema per list |
| `spconnect graph` | Emit the lookup graph (`mermaid` / `json` / `dot`) |
| `spconnect crawl [--resume]` | Full extraction |
| `spconnect sync` | Incremental update, including deletes |
| `spconnect verify-time` | Check that datetimes really are UTC |
| `spconnect stats` | Summarise the landing zone |

`-v` for request logging, `-vv` to capture bodies, `-q` for no narration.

---

## Three things to know before you start

**Datetimes are unverified.** The connector requests `DateInUtc=TRUE`, but that
is a claim about the server's behaviour, not a guarantee. Run
`spconnect verify-time` once and compare against the SharePoint UI before
indexing at scale. If it is wrong, it is fixable offline from `items_raw.jsonl`
— no re-crawl needed.

**Item-level permissions are flattened.** The crawl runs as one identity. Lists
with unique scopes are reported in every summary and in `_manifest.json`. If
technicians currently see different subsets of cases, indexing this widens
access to them. That is a product decision, not a technical detail.

**Partial success is normal.** One failing list does not abort the crawl — that
is deliberate. Always check `_manifest.json` → `counts.lists_failed` before
ingesting.

---

## Development

```bash
pytest                                          # offline, no network
pytest --cov=spconnect --cov-report=term        # ~93% overall
ruff check . && ruff format --check .
mypy src/
```

Everything runs against hand-written fixtures — a two-web, six-list fake farm
that dispatches on the SOAP operation *and* the parameters inside the envelope,
so a crawler sending nonsense cannot pass. `tests/test_live_smoke.py` is the
only test that touches a real server, skipped unless `SP_LIVE_TESTS=1`.

`tests/test_docs.py` asserts that this documentation matches the code: every
setting documented with its real default, every landing-zone field described, no
broken cross-links, and the example reader still compiles. Add a setting and
forget the docs, and the suite says so.

---

## Non-goals

No chunking, embedding or vector-DB writes — the landing zone is the handoff
point. No writes back to SharePoint. No version history, workflows, InfoPath or
web parts. No `Copy.asmx` (it base64-encodes whole files into memory; streaming
`GET` is used instead). No UI.
