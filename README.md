# spconnect — legacy SharePoint extraction connector

Extracts every web, list, list item, field schema and file from a **Windows
SharePoint Services 2.0/3.0 or MOSS 2007** farm into a local landing zone,
ready for a downstream RAG/embedding pipeline.

The target farm predates every modern SharePoint API. There is no CSOM, no
`_api/web` REST, no `ListData.svc`, no Graph, no PnP. The only remote interface
is the classic ASMX SOAP services under `_vti_bin/`, and that is all this
connector speaks. `Office365-REST-Python-Client`, `msal`, `shareplum`'s 365
paths and any Graph SDK will not work against this server and are not used.

**This connector is strictly read-only.** It never writes to SharePoint.

---

## Quick start

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then edit credentials
spconnect probe           # auth + server version. Exits nonzero on failure.
spconnect discover        # webs + lists inventory. Fast. Run this second.
spconnect schema          # field schemas for every in-scope list
spconnect graph --format mermaid   # <- look at this before crawling anything
spconnect crawl --dry-run # size the job
spconnect crawl           # the real extraction
spconnect stats           # what landed
```

`spconnect crawl --resume` continues an interrupted crawl.
`spconnect sync` applies incremental changes, including deletes.

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated list) and are
also settable as real environment variables and, for the common ones, as CLI
flags.

Precedence, highest first:

```
CLI flag  >  environment variable  >  .env  >  built-in default
```

`.env` is in `.gitignore`. `SP_PASSWORD` is held in a `SecretStr`, redacted in
`repr(settings)`, redacted in `_manifest.json`, and never logged. There is a
unit test asserting this.

### Politeness

`SP_REQUESTS_PER_SECOND` (default 3) rate-limits **every** outbound request,
including file downloads. `SP_CONCURRENCY` (default 2) bounds parallel file
downloads within an item; lists are crawled sequentially so progress logging
and checkpointing stay deterministic. On a twenty-year-old farm the rate limit
matters more than the parallelism.

---

## Commands

| Command | What it does |
|---|---|
| `spconnect probe` | Auth check, server build number, `GetAllSubWebCollection`, `SiteData` liveness. Nonzero exit on failure. |
| `spconnect discover` | Webs + lists inventory only, no items. Writes `webs.json` and each `web.json`. |
| `spconnect schema` | `GetList` per in-scope list; writes `list.json` and rebuilds the graph. |
| `spconnect graph --format mermaid\|json\|dot` | Emit the lookup graph from the cached schemas. |
| `spconnect crawl [--resume]` | Full extraction into the landing zone. |
| `spconnect sync` | Incremental via change tokens; applies updates **and deletes**. |
| `spconnect verify-time --list X --item N` | Raw wire value vs decoded UTC vs display form URL. |
| `spconnect stats` | Summarise the landing zone: lists, items, files, bytes, errors. |

Global flags: `--env-file`, `--log-level`, `--log-format`, `--dry-run`,
`--base-url`, `--landing-dir`, `--include-webs`, `--exclude-webs`,
`--include-lists`, `--exclude-lists`, `--include-hidden-lists`,
`--include-document-libraries`, `--download-files`, `--page-size`.

`--dry-run` performs read-only discovery and prints what *would* be crawled —
list titles, item counts, estimated request count and wall time — without
fetching a single item. Use it to size the job before pointing this at
production.

---

## Landing zone contract

**This layout is a stable contract.** The downstream pipeline is written
against it. Do not change it without updating this section and the spec.

```
landing/
├── _manifest.json          # crawl metadata: times, server version, redacted
│                           # config snapshot, counts, errors[]
├── _state.json             # resumable checkpoints + per-list change tokens
├── _graph.json             # lookup graph, machine-readable
├── _graph.mmd              # same graph, Mermaid `graph LR`
├── webs.json               # flat inventory of all discovered webs
└── webs/
    └── {web_slug}/                       # slug = host + URL path, sanitised
        ├── web.json
        └── lists/
            └── {list_guid}/              # lowercase, unbraced
                ├── list.json             # metadata + full field schema
                ├── items.jsonl           # one decoded item per line
                ├── items_raw.jsonl       # one raw ows_ dict per line
                └── files/
                    └── {item_id}/
                        └── {filename}
```

### `items.jsonl`

One JSON object per line, flushed per line:

```json
{
  "doc_id": "{web_id}:{list_guid}:{item_id}",
  "web_url": "http://sp/sites/service",
  "web_id": "1c4e…",
  "list_guid": "{A1B2…}",
  "list_title": "Servicefälle",
  "item_id": 4711,
  "display_url": "http://sp/sites/service/Lists/Cases/DispForm.aspx?ID=4711",
  "content_type": "Item",
  "created": "2009-03-14T08:11:00Z",
  "modified": "2011-07-02T15:43:00Z",
  "is_folder": false,
  "file_ref": "sites/service/Lists/Cases/4711_.000",
  "file_name": "4711_.000",
  "fields": { "…decoded, keyed by internal name…" },
  "field_display_names": { "Kunde": "Kunde", "Case_x0020_Number": "Case Number" },
  "attachments": [
    {"filename": "foto.jpg", "url": "http://sp/…/foto.jpg",
     "local_path": "files/4711/foto.jpg", "bytes": 20481,
     "sha256": "…", "downloaded": true, "skip_reason": null}
  ]
}
```

Notes for whoever writes the downstream pipeline:

- **`doc_id` is stable across runs.** Upsert on it. It is never derived from
  row position or crawl order.
- `web_id` is a **synthetic but deterministic** id: `uuid5(NAMESPACE_URL,
  normalised_web_url)`. None of the seven permitted SOAP operations returns a
  real web GUID, so this stands in for one. It is stable as long as the web URL
  is; if the farm is ever moved to a new hostname, ids change.
- `display_url` is a deep link into the SharePoint display form. RAG answers
  should cite it — that is what makes people trust the new system.
- `fields` keys are **internal names, still `_xHHHH_`-escaped** (e.g.
  `Case_x0020_Number`). That is the join key. `field_display_names` maps them to
  the German labels users recognise.
- `local_path` in `attachments` is relative to the list directory.
- `items_raw.jsonl` holds the same rows as the raw `ows_*` strings (minus
  `ows_MetaInfo`). If the decoder turns out to be wrong about something, the
  raw capture means it can be fixed **without touching the server again**.

### Decoded value shapes

| Field type | Wire | JSON |
|---|---|---|
| `Text`, `Note` | raw string | `"…"` (Note may contain HTML) |
| `Number`, `Currency` | `1234.500000000000` | `1234.5` |
| `Counter`, `Integer` | `42` | `42` |
| `Boolean` | `1`/`0` | `true`/`false` |
| `DateTime` | `2019-04-03T14:22:11Z` | `"2019-04-03T14:22:11Z"` (UTC) |
| `Choice` | raw string | `"…"` |
| `MultiChoice` | `;#Reparatur;#Garantie;#` | `["Reparatur","Garantie"]` |
| `Lookup` | `42;#Müller GmbH` | `{"id":42,"value":"Müller GmbH"}` |
| `LookupMulti` | `42;#Müller;#57;#Beta AG` | `[{"id":42,…},{"id":57,…}]` |
| `User` | `12;#CONTOSO\jdoe` | `{"id":12,"value":"CONTOSO\\jdoe"}` |
| `UserMulti` | same, repeated | list of the above |
| `URL` | `http://x, Anzeigetext` | `{"url":"http://x","description":"Anzeigetext"}` |
| `Calculated` | `float;#1234.5` | `1234.5` (prefix stripped, then decoded) |
| `Attachments` | `1`/`0` | `true`/`false` |
| `File` | `1;#Handbuch.pdf` | `{"id":1,"value":"Handbuch.pdf"}` |

`FileRef` and `FileLeafRef` are lookup-encoded on the wire even though the
schema types the latter as `File`; both decode to the `{id, value}` shape. The
top-level `file_ref` / `file_name` keys on each record carry just the string,
which is usually what you want.

**Always use the numeric lookup `id`, not the display string.** The display
string is a denormalised snapshot written when the row was last saved; it may
no longer match the current value of the target row. The `id` is the real
foreign key.

`ows_MetaInfo` is skipped entirely — it is a property bag with its own internal
format and no value here.

### `_graph.json` / `_graph.mmd`

The lookup graph is the **recovered data model of the CRM** and the most
valuable artifact of the crawl.

- **Nodes** are lists, keyed by GUID, carrying web URL, title, item count and
  base type.
- **Edges** are `Lookup`/`LookupMulti` columns: source list → target list, with
  the field's internal name, display name, `ShowField`, and whether it is
  multi-valued.
- `List="Self"` is resolved to the containing list (`self_reference: true`).
- An edge whose target GUID matches no crawled list is kept and marked
  `dangling: true` rather than dropped — it means the target is out of scope or
  the credential cannot read it, which is information, not noise.

Use it to decide which lists to denormalise into which documents.

---

## Incremental sync

`spconnect sync` uses `Lists.GetListItemChangesSinceToken`. The first pass per
list stores a `LastChangeToken` in `_state.json`; subsequent runs send it and
receive only inserts, updates and deletes.

Deletes arrive as `<Id ChangeType="Delete">123</Id>` and **are applied** — the
matching lines are removed from both JSONL files. A vector DB full of cases that
no longer exist is worse than one that is slightly stale.

If the token is rejected (it can expire, or be invalidated by a farm
operation), or the server build predates WSS 3.0, the list falls back to a full
crawl and a WARNING is logged and recorded in the manifest.

---

## Resumability

`_state.json` holds per list GUID: web URL, last full crawl time, `last_item_id`,
`change_token`, `status` (`pending`/`in_progress`/`complete`/`failed`) and the
last error.

It is written after **every page**, atomically (temp file + `os.replace`).

Paging is on the `ID` counter (`ID > last_id ORDER BY ID ASC`), not on
`ListItemCollectionPositionNext`. Token paging works but is not resumable
across process restarts. ID paging is deterministic, resumable, and survives a
crashed crawl.

`spconnect crawl --resume` skips lists already marked `complete`, truncates any
rows beyond the last checkpoint (including a half-written trailing line from a
killed process), and continues from `last_item_id`. No duplicates, no gaps.

---

## Error policy

- **A failure in one list does not abort the crawl.** It is recorded in
  `_manifest.json` under `errors[]`, the list is marked `failed` in state, and
  the crawl moves on.
- **Authentication failures (401/403) abort immediately.** A misconfigured
  credential producing 87 "failed" lists is a useless artifact.
- 404 is never retried. 500/502/503/504 and connection/timeout errors are
  retried with exponential backoff — except a 500 carrying a `<soap:Fault>`,
  which is an application error and is surfaced as `SharePointSoapFault`
  immediately.
- The final summary prints lists succeeded/failed, items written, files
  downloaded, files skipped and why, dangling lookup edges and decoder warnings.

---

## Verify on first contact with the real server

These are genuinely uncertain. The code degrades gracefully rather than
crashing, but somebody has to check them once, against the real farm.

1. **The exact SharePoint version.** Behaviour differs between WSS 2.0 and 3.0.
   The `MicrosoftSharePointTeamServices` response header drives this; nothing is
   hardcoded. Major `6` = WSS 2.0/SPS 2003, `12` = WSS 3.0/MOSS 2007, `14` =
   2010, `15` = 2013. Below 12, `GetListItemChangesSinceToken` may not exist and
   `sync` falls back to full crawls unconditionally.
2. **That `DateInUtc` behaves as documented on this build.** Run
   `spconnect verify-time --list "Servicefälle" --item 4711`, open the printed
   display form URL, and compare. If they disagree, every datetime in the
   landing zone is off by the server's UTC offset — fixable from
   `items_raw.jsonl` without re-crawling, but you want to know early.
3. **That authentication is NTLM.** Installs of this era also use Basic over
   HTTP, Kerberos, or forms-based auth. NTLM and Basic are supported
   (`SP_AUTH_MODE`). Anything else is a follow-up.
4. **That `GetAllSubWebCollection` returns everything.** It returns what *this
   credential* can read. `spconnect probe` prints the count prominently — if it
   looks low, the account is missing permissions somewhere, and lists you never
   see will silently not be in the RAG index.
5. **Item-level permissions.** If different technicians currently see different
   cases, this connector flattens that distinction: it crawls as one identity.
   Any list reporting `HasUniqueScopes="True"` is called out in the final
   summary and recorded in `_manifest.json`. The downstream vector DB will not
   preserve per-item security.

Two more worth knowing:

6. **Legacy TLS.** `SP_ALLOW_LEGACY_TLS=true` mounts an adapter allowing TLS 1.0
   and `DEFAULT@SECLEVEL=0` ciphers, with certificate verification off when
   `SP_VERIFY_SSL=false`. A WARNING is logged whenever this is active. Many
   installs of this vintage are plain HTTP, where none of it applies.
7. **`ViewFields`.** With an empty `viewName` *and* empty `viewFields`,
   SharePoint returns the **default view's** columns, not all of them. The
   crawler therefore always sends an explicit `<ViewFields>` built from the
   list's own schema, so no column is quietly missing.

---

## Development

```bash
pip install -e ".[dev]"
pytest                       # entirely offline, fixture-driven
pytest --cov=spconnect --cov-report=term-missing
ruff check . && ruff format --check .
mypy src/
```

`tests/test_live_smoke.py` is skipped unless `SP_LIVE_TESTS=1`. When enabled it
probes the version header, calls `GetAllSubWebCollection`, calls
`GetListCollection` on the first web, and pulls exactly one page of one list. It
is read-only and does not write to the landing zone — the operator's five-minute
confidence check against the real server.

Everything else runs against hand-written fixtures in `tests/fixtures/` with no
network access. Those fixtures deliberately contain umlauts, `ß`, `_x0020_`
escaping, multi-lookups, `;#`-wrapped multi-choice values, calculated fields
with type prefixes, HTML in a Note field, a dangling lookup, an empty list, a
page boundary and a SOAP fault.

### Module map

| Module | Responsibility |
|---|---|
| `config.py` | pydantic-settings, precedence, redaction, structlog setup |
| `transport.py` | session, NTLM/Basic, legacy TLS, rate limit, retries, version probe |
| `soap.py` | envelope build/parse, XML-fragment params, fault handling |
| `services/webs.py` | `Webs.GetAllSubWebCollection` |
| `services/lists.py` | `GetListCollection`, `GetList`, `GetListItems`, `GetListItemChangesSinceToken`, `GetAttachmentCollection` |
| `services/sitedata.py` | optional liveness probe only |
| `decode.py` | `ows_*` value decoding — the core logic |
| `schema.py` | field parsing, `_xHHHH_` escaping, lookup graph, renderers |
| `files.py` | streaming authenticated downloads, hashing, skip policy |
| `landing.py` | the landing zone contract |
| `state.py` | atomic checkpoints and change tokens |
| `crawl.py` | orchestration |
| `cli.py` | typer commands |

## Non-goals

No chunking, embedding or vector DB writes — the landing zone is the handoff
point. No writes back to SharePoint. No version history, workflows, InfoPath or
web parts. No `Copy.asmx` (it base64-encodes whole files into memory; streaming
`GET` is used instead). No UI.
