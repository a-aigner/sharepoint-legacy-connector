# Architecture

**Audience:** whoever maintains or extends this.

---

## The shape of the problem

A twenty-year-old SharePoint farm used as a CRM. Service cases are list items,
heavily cross-referenced through lookup columns, with attachments and document
libraries. The extraction has to be read-only, resumable, and honest about what
it could not read.

Two constraints drive every design decision:

1. **Re-crawling is expensive.** The server is slow, old, and in production.
   Anything that can be captured once and fixed offline should be.
2. **Partial success is the normal case.** Some lists will be unreadable, some
   will be throttled, some will have odd data. A design that treats any failure
   as fatal produces nothing useful.

---

## Module map

```
cli.py          typer commands, step narration, exit codes
  └── crawl.py            orchestration: discover → schema → graph → items → files
        ├── services/
        │     ├── webs.py       Webs.asmx — site discovery (+ WSS 2.0 fallback)
        │     ├── lists.py      Lists.asmx — lists, schema, items, changes, attachments
        │     ├── odata.py      ListData.svc — the optional REST item backend
        │     └── sitedata.py   SiteData.asmx — liveness probe only
        ├── soap.py             envelope build/parse, faults, XML-fragment params
        ├── decode.py           ows_ wire-value decoding
        ├── schema.py           field parsing, _xHHHH_ escaping, the lookup graph
        ├── files.py            streaming downloads, hashing, skip policy
        ├── landing.py          the on-disk contract
        └── state.py            atomic checkpoints, change tokens

transport.py    session, auth, legacy TLS, rate limit, retries, probes
config.py       settings, precedence, secret scrubbing, log setup
console.py      step narration
trace.py        private-file body capture
models.py       every pydantic model that reaches disk
```

Dependencies point downward. `transport`, `config`, `console`, `trace` and
`models` are leaves; nothing in `services/` knows about `crawl` or `cli`.

---

## Request path

```
cli → Crawler → ListsService → SoapClient → Transport → requests.Session
                                  ↓                            ↓
                              soap.py                    rate limiter
                          build_envelope                 retry policy
                          parse_response                 NTLM/SSPI auth
                                  ↓
                         SharePointSoapFault
                         SoapResponseError
```

Every layer has one job:

- **`Transport`** owns the session. NTLM is *connection*-oriented — it
  authenticates the TCP connection, not the request — so everything goes through
  one pooled session with keep-alive on. Disabling pooling would make every call
  re-handshake.
- **`soap.py`** builds envelopes with `lxml`, never string concatenation. Three
  parameters (`query`, `viewFields`, `queryOptions`) take **XML fragments as
  element content**; building them as real elements makes escaping bugs
  structurally impossible.
- **`services/*`** translate one endpoint into typed models. No policy.
- **`crawl.py`** holds all the policy: scope, ordering, error isolation,
  checkpointing.

---

## Error taxonomy

| Exception | Meaning | Retried? | Aborts the run? |
|---|---|---|---|
| `AuthenticationError` | 401/403 | **No** | **Yes** |
| `NotFoundError` | 404 | No | No |
| `RetryableTransportError` | 5xx, timeout, connection reset | Yes, exponential | No |
| `SharePointSoapFault` | `<soap:Fault>` — application error | No | No |
| `SoapResponseError` | 200 but not the expected SOAP | No | No |
| `ODataUnavailable` | `ListData.svc` absent or broken | No | No — falls back to SOAP |
| `IntegratedAuthUnavailable` | No SSPI/Kerberos provider | No | **Yes** |
| `CrawlAborted` | Wraps a fatal auth failure | — | **Yes** |

Two deliberate asymmetries:

**A 500 carrying a `<soap:Fault>` is not retried.** It is the application
talking, not a flaky server. The transport sniffs the body to tell them apart —
using a regex, not a substring search, because `DefaultViewUrl` (an attribute on
every `<List>` element) contains the word "fault".

**Auth failures abort everything.** One bad credential producing 87 "failed"
lists is a useless artifact.

---

## Paging

`ID > last_id ORDER BY ID ASC`, `rowLimit = SP_PAGE_SIZE`, repeat until a page
returns fewer than `rowLimit` rows.

Chosen over `ListItemCollectionPositionNext` because the token is not resumable
across process restarts. Two consequences fell out of that choice:

- **Resumability.** `last_id` is a checkpoint that survives a crash.
- **Threshold safety.** `ID` is always indexed, so SharePoint 2010+ seeks the
  index instead of scanning — which is the standard mitigation for the 5000-item
  list view threshold. The resumability decision happened to solve a problem it
  was not aimed at.

**Runaway guard:** if a page's maximum ID is not greater than `last_id`, the
list aborts. A server ignoring the filter would otherwise loop until the disk
fills.

---

## Two item backends

`SP_API_MODE` selects what fetches **list items**, and nothing else.

|  | `soap` (default) | `odata` |
|---|---|---|
| Endpoint | `Lists.asmx` `GetListItems` | `_vti_bin/ListData.svc` |
| Builds | 2003 → 2016 | 2010+ |
| Values | `ows_` strings → `decode.py` | typed JSON → `ODataRowMapper` |

These stay on SOAP in **both** modes, because OData has no equivalent:

- **web discovery** — no OData counterpart exists,
- **list discovery and field schema** — `$metadata` exposes EDM associations
  between sanitised entity-set names, not list GUIDs, internal field names, or
  `ShowField`/`List=` lookup targets,
- **deletes** — OData has no change feed.

So `odata` is a second source for one step, not a replacement stack. Both write
byte-identical `doc_id`s; there is a regression test, because the downstream
pipeline upserts on them and a backend switch must not duplicate every document.

The two backends check each other. The first side-by-side run caught a real bug:
OData v2 serialises `Edm.Decimal` as a *string* to protect precision, so `Kosten`
came back `"1234.5000000000000"` where SOAP gave `1234.5`. Now coerced via the
schema.

---

## Version-driven behaviour

Nothing is hardcoded to a SharePoint version. The
`MicrosoftSharePointTeamServices` response header drives four capability flags:

| Property | Threshold | Effect when false |
|---|---|---|
| `supports_change_tokens` | major ≥ 12 | `sync` does full crawls |
| `supports_all_sub_web_collection` | major ≥ 12 or unknown | Recursive `GetWebCollection` walk |
| `has_list_view_threshold` | major ≥ 14 | No 5000-item warnings |
| *(product name)* | — | Reported as "unknown build major N" |

Unknown versions get the benefit of the doubt: try the modern path, fall back if
the server disagrees. The `GetWebCollection` fallback also triggers on a *fault
or non-SOAP body* from `GetAllSubWebCollection`, so a version header that lies
costs nothing.

---

## Decoding

`decode.py` is the highest-risk module: a bug here corrupts twenty years of
history silently. Three properties keep it safe:

1. **Total.** Every decoder returns a best-effort value and logs a warning.
   None raises. One bad row must not abort a list.
2. **Table-driven.** One function per wire format, one dispatch table, one test
   row per table row. 95% coverage.
3. **Reversible.** Every row is persisted *both* decoded and raw. A decoder bug
   is fixable offline from `items_raw.jsonl`, without touching the server.

The delimiter is `;#`. Multi-value fields wrap in it, so a naive `split(";#")`
yields empty strings at both ends. A display value containing a literal `;#`
produces an odd token count — detected, warned about with list/item/field, and
handled best-effort rather than silently corrupting.

**Lookups always keep the numeric id.** The display half is a denormalised
snapshot from save time and may already be stale.

---

## The lookup graph

`schema.py` builds it from the `List=` and `ShowField` attributes of every
`Lookup`/`LookupMulti` column. This is the recovered data model of a CRM nobody
documented, and the most valuable single artifact of the crawl.

- `List="Self"` resolves to the containing list.
- A target GUID matching no crawled list is kept as a **dangling** edge rather
  than dropped. Knowing a foreign key leaves your dataset is information.
- `User` columns are *not* edges — they point at the hidden user list, which
  would be noise.

---

## Landing zone and resume

`landing.py` owns every path. `ListWriter` appends to two JSONL files with a
flush per line — lists here run to six figures, and accumulating in memory to
"write at the end" would mean losing hours to one OOM.

Resume relies on one invariant: **`items.jsonl` contains exactly the items with
`id <= last_item_id`**. `truncate_after()` enforces it by rewriting the file,
dropping rows past the checkpoint and any unparseable trailing line from a
killed process.

The two files use different id keys — `item_id` in the decoded file, `ID`/`Id`
in the raw one. Getting that wrong once meant truncation matched nothing and
wiped `items_raw.jsonl` on resume; it is now covered by tests.

`state.py` writes atomically: temp file, `fsync`, `os.replace`. A corrupt state
file is moved aside rather than being fatal.

---

## Secrets

Two layers, in order of strength:

1. **`SP_AUTH_MODE=integrated`** — a control. The process authenticates as its
   own identity; no password is read, stored, or held. Nothing to leak.
2. **Scrubbing** — a mitigation, for when a password does exist. See
   [security.md](security.md) for exactly what it covers and what it cannot.

Bodies are captured to a `0600` file, never to the log stream.

---

## Testing

Everything runs offline against hand-written fixtures. `FakeFarm` in
`tests/conftest.py` is a two-web, six-list SharePoint farm that dispatches on the
SOAP operation **and the parameters inside the envelope** — a mock that ignored
the request body would happily pass a crawler sending nonsense.

Fixtures deliberately contain: German umlauts and `ß`, `_x0020_` escaping,
three-value multi-lookups, `;#`-wrapped multi-choice, calculated fields with
type prefixes, HTML with escaped ampersands in a Note, a dangling lookup, an
empty list, a page boundary, a SOAP fault, a throttle fault, an FBA login page,
and items with and without attachments.

`tests/test_live_smoke.py` is the only test that touches a real server, skipped
unless `SP_LIVE_TESTS=1`.

### Where to add tests

| Changing… | Test in… |
|---|---|
| A wire format | `test_decode.py` — one parametrised row |
| Field parsing or the graph | `test_schema.py` |
| Envelopes or faults | `test_soap.py` |
| Paging behaviour | `test_paging.py` |
| The output contract | `test_landing.py` **and** `test_crawl.py` |
| The REST backend | `test_odata.py` — including a cross-backend equivalence test |
| Logging or secrets | `test_logging.py` |

---

## Extending it

**A new field type:** add a decoder to `decode.py`, register it in
`_SIMPLE_DECODERS`, add a row to the table in `test_decode.py` and to the table
in [landing-zone.md](landing-zone.md#value-shapes).

**A new SOAP operation:** the spec deliberately limits this to seven. If you add
an eighth, wrap it in `services/`, keep policy in `crawl.py`, and add a fixture.

**A new landing-zone field:** it is a contract. Add it to `models.py`, document
it in [landing-zone.md](landing-zone.md), note it in
[decisions.md](decisions.md), and keep it additive — downstream readers must not
break.

**Non-goals, deliberately:** no chunking or embedding (the landing zone is the
handoff), no writes to SharePoint, no version history, no workflows or InfoPath,
no `Copy.asmx` (it base64-encodes whole files into memory), no UI.
