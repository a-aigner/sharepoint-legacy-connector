# Build Spec: `spconnect` — Legacy SharePoint Extraction Connector

**Audience:** Claude Code (implementation agent)
**Deliverable:** A Python package + CLI that extracts all list items, metadata, schemas and files from a legacy on-premises SharePoint into a local landing zone, ready for a downstream RAG/embedding pipeline.

---

## 0. Context

The target is a **Windows SharePoint Services 3.0 / MOSS 2007** farm (possibly WSS 2.0 / SPS 2003) that has been in production for ~20 years. It is used as a **CRM**: service cases are recorded as list items with heavy use of lookup columns between lists, plus attachments and document libraries.

**This system predates every modern SharePoint API.** There is no CSOM, no `_api/web` REST, no `ListData.svc` OData, no Microsoft Graph, no PnP PowerShell. The **only** remote interface is the classic ASMX SOAP web services under `_vti_bin/`.

Do not attempt to use `Office365-REST-Python-Client`, `msal`, `shareplum`'s 365 code paths, or any Graph SDK. They will not work.

### Goals

1. Enumerate every web (site/subsite), every list, and every list item reachable by the configured credentials.
2. Capture each list's **field schema**, including the lookup relationships that form the CRM's implicit foreign-key graph.
3. Decode SharePoint's `ows_*` wire encodings into clean typed JSON.
4. Download list item attachments and document library files.
5. Write everything to a **landing zone on local disk** in a stable, documented format.
6. Support **incremental re-sync** so the downstream vector DB can be kept current.

### Non-goals — do NOT build these

- No chunking, embedding, or vector DB writes. The landing zone is the handoff point.
- No writes of any kind back to SharePoint. This connector is **strictly read-only**.
- No version history extraction. Only current versions matter.
- No workflow, InfoPath, or web part extraction.
- No UI.

---

## 1. Tech stack

- **Python 3.11+**
- `requests` — HTTP
- `requests-ntlm` — NTLM authentication
- `lxml` — XML parsing (namespace handling in these responses is awkward; `lxml` handles it better than `ElementTree`)
- `pydantic` v2 + `pydantic-settings` — config and data models
- `typer` — CLI
- `structlog` (or stdlib `logging` with JSON formatter) — logging
- `tenacity` — retry policy

**Dev:** `pytest`, `pytest-cov`, `responses` (HTTP mocking), `ruff`, `mypy`.

Use `uv` for dependency management if available, otherwise `pip` + `pyproject.toml`.

---

## 2. Repository layout

```
spconnect/
├── pyproject.toml
├── README.md
├── .env.example
├── .gitignore                    # must include .env
├── src/
│   └── spconnect/
│       ├── __init__.py
│       ├── config.py             # pydantic-settings
│       ├── transport.py          # requests session, auth, legacy TLS, retries
│       ├── soap.py               # SOAP envelope build/parse, fault handling
│       ├── services/
│       │   ├── __init__.py
│       │   ├── webs.py           # Webs.asmx
│       │   ├── lists.py          # Lists.asmx
│       │   └── sitedata.py       # SiteData.asmx (optional, version probe)
│       ├── models.py             # Web, ListInfo, FieldDef, Item
│       ├── decode.py             # ows_ value decoding  <-- core logic
│       ├── schema.py             # field schema parsing + lookup graph
│       ├── files.py              # attachment + document library download
│       ├── crawl.py              # orchestration
│       ├── state.py              # checkpoints, change tokens
│       ├── landing.py            # landing zone writer
│       └── cli.py
└── tests/
    ├── conftest.py
    ├── fixtures/                 # captured/synthetic SOAP XML
    │   ├── webs_getallsubwebcollection.xml
    │   ├── lists_getlistcollection.xml
    │   ├── lists_getlist_cases.xml
    │   ├── lists_getlistitems_page1.xml
    │   ├── lists_getlistitems_page2.xml
    │   ├── lists_getlistitemchangessincetoken.xml
    │   ├── lists_getattachmentcollection.xml
    │   └── soap_fault.xml
    ├── test_decode.py
    ├── test_schema.py
    ├── test_soap.py
    ├── test_webs.py
    ├── test_lists.py
    ├── test_paging.py
    ├── test_crawl.py
    ├── test_landing.py
    └── test_live_smoke.py        # skipped unless SP_LIVE_TESTS=1
```

---

## 3. Configuration

All configuration via `.env` in the project root, loaded with `pydantic-settings` (`SettingsConfigDict(env_file=".env")`). Every setting must also be overridable by a real environment variable and by a CLI flag, in that precedence order: CLI flag > env var > `.env` > default.

Ship a `.env.example` with this content and inline comments:

```dotenv
# ---- Connection ----
# Root URL of the SharePoint web application or site collection. No trailing slash.
SP_BASE_URL=http://sharepoint.intern.example.de

# ntlm | basic | anonymous
SP_AUTH_MODE=ntlm

# For NTLM use DOMAIN\username (escape the backslash if your loader needs it)
SP_USERNAME=CONTOSO\svc_extract
SP_PASSWORD=changeme

# ---- Transport quirks (legacy servers) ----
# Old IIS may only offer TLS 1.0/1.1 with weak ciphers that modern OpenSSL rejects.
SP_ALLOW_LEGACY_TLS=true
SP_VERIFY_SSL=false
SP_TIMEOUT_SECONDS=120
SP_MAX_RETRIES=5
SP_BACKOFF_BASE_SECONDS=2

# ---- Politeness: this server is 20 years old. Be gentle. ----
SP_CONCURRENCY=2
SP_REQUESTS_PER_SECOND=3

# ---- Crawl scope ----
# Comma-separated. Empty = all.
SP_INCLUDE_WEBS=
SP_EXCLUDE_WEBS=
SP_INCLUDE_LISTS=
SP_EXCLUDE_LISTS=
SP_INCLUDE_HIDDEN_LISTS=false
SP_INCLUDE_DOCUMENT_LIBRARIES=true

# ---- Paging ----
SP_PAGE_SIZE=200

# ---- Files ----
SP_DOWNLOAD_FILES=true
SP_MAX_FILE_MB=100
SP_SKIP_EXTENSIONS=.exe,.dll,.msi,.iso

# ---- Output ----
SP_LANDING_DIR=./landing
SP_STATE_FILE=./landing/_state.json

# ---- Logging ----
SP_LOG_LEVEL=INFO
SP_LOG_FORMAT=console        # console | json

# ---- Tests ----
# Set to 1 to enable tests that hit the real server.
SP_LIVE_TESTS=0
```

`.env` must be in `.gitignore`. The config module must **never** log `SP_PASSWORD`; add a `__repr__` that redacts it, and a unit test asserting the password does not appear in `repr(settings)`.

---

## 4. Transport layer (`transport.py`)

Build a single reusable `requests.Session`.

**NTLM:** `HttpNtlmAuth(username, password)` from `requests_ntlm`. NTLM is a **connection-oriented** scheme — it authenticates the TCP connection, not the request. The session must use keep-alive and must not disable connection pooling, or every request will re-handshake (slow) or fail.

**Legacy TLS:** when `SP_ALLOW_LEGACY_TLS=true`, mount a custom `HTTPAdapter` with an `ssl.SSLContext` that sets:

```python
ctx = ssl.create_default_context()
ctx.minimum_version = ssl.TLSVersion.TLSv1
ctx.set_ciphers("DEFAULT@SECLEVEL=0")
ctx.check_hostname = False  # only when SP_VERIFY_SSL=false
ctx.verify_mode = ssl.CERT_NONE  # only when SP_VERIFY_SSL=false
```

Log a clear WARNING when legacy TLS is active. Many installs of this vintage are plain HTTP, in which case none of this applies.

**Retries:** use `tenacity` with exponential backoff on `ConnectionError`, `Timeout`, and HTTP 500/502/503/504. Do **not** retry 401/403 (credential problem — fail fast and loudly) or 404.

**Rate limiting:** a simple token-bucket or sleep-based limiter honouring `SP_REQUESTS_PER_SECOND`, applied to every outbound request including file downloads.

**Version probe:** a `GET` or `HEAD` against any page on the server returns a response header `MicrosoftSharePointTeamServices` with a build number (e.g. `12.0.0.6421`). Major version `6`=WSS 2.0/SPS 2003, `12`=WSS 3.0/MOSS 2007, `14`=2010, `15`=2013. Capture and log this at startup; store it in the landing zone manifest. If the major version is `6`, log a prominent WARNING that several operations in this spec (notably `GetListItemChangesSinceToken`) may be unavailable.

---

## 5. SOAP layer (`soap.py`)

All services live at `{web_url}/_vti_bin/{Service}.asmx`, **relative to each web**, not the site collection root. Crawling subsites means constructing a fresh endpoint per web.

Every request:

- `Content-Type: text/xml; charset=utf-8`
- `SOAPAction: "http://schemas.microsoft.com/sharepoint/soap/{OperationName}"` (note the surrounding quotes)
- Envelope:

```xml
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
               xmlns:xsd="http://www.w3.org/2001/XMLSchema"
               xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{OperationName} xmlns="http://schemas.microsoft.com/sharepoint/soap/">
      <!-- parameters -->
    </{OperationName}>
  </soap:Body>
</soap:Envelope>
```

**Critical:** several parameters (`query`, `viewFields`, `queryOptions`) take **XML fragments as element content**. Build them as real child elements, not escaped strings. If you build them by string concatenation, any user-supplied value must be XML-escaped or the request will be malformed.

**Fault handling:** SharePoint returns HTTP 500 with a `<soap:Fault>` body containing `<faultstring>` and often a `<detail><errorstring>`. Parse these into a typed `SharePointSoapFault` exception carrying the fault string, error code, and the operation name. A SOAP fault is **not** a transport error — do not retry it blindly.

Consider generating typed clients with `zeep` against the WSDLs (available at `{web}/_vti_bin/Lists.asmx?WSDL`). Hand-rolled envelopes with `lxml` are also acceptable and give more control over the XML-fragment parameters, which `zeep` sometimes mangles. **Pick one approach and be consistent.** If `zeep` fights the fragment parameters, fall back to hand-rolled.

---

## 6. Operations to implement

Only these seven are needed. Do not implement anything else.

### 6.1 `Webs.GetAllSubWebCollection` — site discovery

Endpoint: `{base}/_vti_bin/Webs.asmx`. No parameters. Returns every web beneath the called web, recursively, that the caller can read.

```xml
<GetAllSubWebCollectionResult>
  <Webs xmlns="http://schemas.microsoft.com/sharepoint/soap/">
    <Web Title="Service" Url="http://sp/sites/service"/>
    <Web Title="Cases 2008" Url="http://sp/sites/service/cases2008"/>
  </Webs>
</GetAllSubWebCollectionResult>
```

Deduplicate and normalise URLs (strip trailing slashes, lowercase the host, preserve path case).

### 6.2 `Lists.GetListCollection` — list discovery per web

Endpoint: `{web}/_vti_bin/Lists.asmx`. No parameters. Returns `<List .../>` elements with attributes including `ID` (GUID, braced), `Title`, `BaseType`, `ServerTemplate`, `ItemCount`, `Hidden`, `RootFolder`, `EnableAttachments`, `Created`, `Modified`, `DefaultViewUrl`.

`BaseType`: `0` = generic list, `1` = document library, `4` = survey, `5` = issue tracking.

Filter out hidden lists unless `SP_INCLUDE_HIDDEN_LISTS=true`. Always skip these system lists by title regardless: `Master Page Gallery`, `Web Part Gallery`, `List Template Gallery`, `Site Template Gallery`, `User Information List`, `Solution Gallery`, `Style Library`, `Form Templates`, `Workflow History`, `TaskAttachments`, `Reporting Metadata`, `Reporting Templates`, `Converted Forms`. Make this list a module-level constant so it is easy to extend.

### 6.3 `Lists.GetList` — schema

Parameter: `listName` (the GUID including braces is more reliable than the title). Returns the `<List>` element with a `<Fields>` child containing one `<Field>` per column.

Field attributes to capture:

| Attribute | Meaning |
|---|---|
| `ID` | Field GUID |
| `Name` | **Internal name** — this is what `ows_` attributes use |
| `StaticName` | Usually equals `Name` |
| `DisplayName` | What users see (likely German) |
| `Type` | `Text`, `Note`, `Number`, `Currency`, `DateTime`, `Boolean`, `Choice`, `MultiChoice`, `Lookup`, `LookupMulti`, `User`, `UserMulti`, `Calculated`, `URL`, `Counter`, `Attachments`, `Computed`, … |
| `Required` | `TRUE`/`FALSE` |
| `Hidden`, `ReadOnly` | Booleans |
| `List` | **Lookup only** — GUID of the target list, or the literal `Self` |
| `ShowField` | **Lookup only** — which column of the target is displayed |
| `Mult` | `TRUE` for multi-value lookups |
| `ColName` | Physical DB column; capture it for diagnostics, don't rely on it |
| `Format` | e.g. `DateOnly` vs `DateTime` for `DateTime` fields |
| `ResultType` | **Calculated only** — the type of the computed value |

`Choice`/`MultiChoice` fields carry a `<CHOICES><CHOICE>…</CHOICE></CHOICES>` child. `Calculated` fields carry a `<Formula>` child — capture it verbatim.

**Internal name escaping:** internal names encode non-alphanumeric characters as `_xHHHH_`. A column displayed as `Case Number` has internal name `Case_x0020_Number`. Implement `unescape_internal_name()` and `escape_internal_name()` and test both round-trip.

### 6.4 `Lists.GetListItems` — full pull

Parameters: `listName`, `viewName` (empty string = no view, return all fields), `query`, `viewFields`, `rowLimit`, `queryOptions`, `webID` (empty).

Use these query options **always**:

```xml
<QueryOptions>
  <DateInUtc>TRUE</DateInUtc>
  <IncludeMandatoryColumns>TRUE</IncludeMandatoryColumns>
  <IncludeAttachmentUrls>TRUE</IncludeAttachmentUrls>
  <ViewAttributes Scope="RecursiveAll"/>
</QueryOptions>
```

- `DateInUtc=TRUE` normalises all datetimes to UTC in `yyyy-MM-ddTHH:mm:ssZ` form. **Without this you get server-local time in an ambiguous format.** This is the single most important option here.
- `ViewAttributes Scope="RecursiveAll"` makes document libraries return items inside folders, not just the root.
- `IncludeAttachmentUrls=TRUE` populates `ows_AttachmentUrls`.

Leave `viewFields` empty to get all fields, or send `<ViewFields Properties='TRUE'/>`.

Response shape:

```xml
<listitems xmlns:rs="urn:schemas-microsoft-com:rowset" xmlns:z="#RowsetSchema">
  <rs:data ItemCount="200" ListItemCollectionPositionNext="Paged=TRUE&amp;p_ID=200">
    <z:row ows_ID="1" ows_Title="Getriebeschaden" ows_Kunde="42;#Müller Maschinenbau GmbH" .../>
  </rs:data>
</listitems>
```

**Paging — implement ID-based paging, not token paging.** SharePoint returns a `ListItemCollectionPositionNext` token which you can feed back via `<Paging ListItemCollectionPositionNext="..."/>`. It works, but the token contains `&` characters that must be re-escaped, and it is not resumable across process restarts.

Instead, page on the `ID` counter:

```xml
<Query>
  <Where>
    <Gt><FieldRef Name="ID"/><Value Type="Counter">{last_id}</Value></Gt>
  </Where>
  <OrderBy><FieldRef Name="ID" Ascending="TRUE"/></OrderBy>
</Query>
```

with `rowLimit = SP_PAGE_SIZE`, starting at `last_id = 0`, repeating until a page returns fewer than `rowLimit` rows. This is deterministic, resumable, and survives a crashed crawl. Record `last_id` in the state file after every page.

Note: WSS 3.0 has no 5000-item list view threshold — that arrived in 2010 — so large lists are slow but not blocked.

### 6.5 `Lists.GetListItemChangesSinceToken` — incremental sync

Same parameters as `GetListItems` plus `changeToken`. On the first call omit the token; the response carries a `<Changes LastChangeToken="...">` element. Persist that token per list and send it next time to receive only inserts, updates, and **deletes**.

Deletes appear as `<Id ChangeType="Delete">123</Id>` inside `<Changes>`. **Handle them** — a vector DB full of cases that no longer exist is worse than one that is slightly stale.

If the server rejects the token (it can expire, or be invalidated by a farm operation), the response indicates the token is invalid; fall back to a full `GetListItems` crawl for that list and log a WARNING.

If the version probe reported a major version below 12, guard this call and fall back to full crawl unconditionally.

### 6.6 `Lists.GetAttachmentCollection` — list item attachments

Parameters: `listName`, `listItemID`. Returns:

```xml
<Attachments>
  <Attachment>http://sp/sites/service/Lists/Cases/Attachments/123/foto.jpg</Attachment>
</Attachments>
```

**Only call this when needed.** If `IncludeAttachmentUrls=TRUE` populated `ows_AttachmentUrls`, use that instead — it comes free with the item and saves one round trip per item. Call `GetAttachmentCollection` only as a fallback when `ows_Attachments` indicates attachments exist but `ows_AttachmentUrls` is empty.

### 6.7 Plain HTTP `GET` — file bytes

For both list attachments and document library files, download with an authenticated `GET` on the absolute URL, streaming to disk. For document libraries the URL is in `ows_EncodedAbsUrl`.

`Copy.asmx`'s `GetItem` also works and returns base64 plus metadata in one call, but it loads the whole file into memory as base64. **Prefer streaming GET.** Do not implement `Copy.asmx`.

Skip files whose extension is in `SP_SKIP_EXTENSIONS` or whose size exceeds `SP_MAX_FILE_MB`, and record the skip in the manifest with a reason.

---

## 7. Value decoding (`decode.py`) — the core logic

`GetListItems` returns every value as a string attribute on `<z:row>`. The encodings are undocumented in any single place and are where this project will actually succeed or fail. Implement a decoder dispatching on the field `Type` from the schema, with a raw-string fallback for unknown types.

| Field type | Wire format | Decode to |
|---|---|---|
| `Text`, `Note` | raw string (Note may contain HTML) | `str` |
| `Number`, `Currency` | `1234.500000000000` | `float` |
| `Counter`, `Integer` | `42` | `int` |
| `Boolean` | `1` / `0` | `bool` |
| `DateTime` | `2019-04-03T14:22:11Z` (with `DateInUtc`) | timezone-aware `datetime` |
| `Choice` | raw string | `str` |
| `MultiChoice` | `;#Reparatur;#Garantie;#` | `list[str]` |
| `Lookup` | `42;#Müller Maschinenbau GmbH` | `{"id": 42, "value": "Müller Maschinenbau GmbH"}` |
| `LookupMulti` | `42;#Müller;#57;#Beta AG` | `list[{"id","value"}]` |
| `User` | `12;#CONTOSO\\jdoe` | `{"id": 12, "value": "CONTOSO\\jdoe"}` |
| `UserMulti` | same pattern, repeated | `list[{"id","value"}]` |
| `URL` | `http://example.com, Anzeigetext` | `{"url": ..., "description": ...}` |
| `Calculated` | `float;#1234.5` or `datetime;#2019-04-03 14:22:11` | strip the type prefix, then decode per the prefix |
| `Attachments` | `1` / `0` | `bool` |

**Rules and traps:**

- The delimiter is `;#`. Multi-value fields are wrapped in a leading and trailing delimiter, so naive `split(";#")` yields empty strings at both ends — strip them.
- A literal `;#` inside a display value is theoretically possible and will corrupt parsing. Detect odd-length token lists in lookup parsing and log a WARNING with the list, item ID, and field name rather than silently producing garbage.
- **Always keep the numeric lookup ID, not just the display string.** The display string is a denormalised snapshot written at save time and may no longer match the current value of the target row.
- `Calculated` fields prefix the value with its own type name and `;#`. Handle at minimum `float`, `int`, `datetime`, `boolean`, `string`.
- `ows_MetaInfo` is a property bag with its own internal format. **Skip it entirely.**
- Useful system fields to retain: `ows_ID`, `ows_UniqueId`, `ows_GUID`, `ows_Created`, `ows_Modified`, `ows_Author`, `ows_Editor`, `ows_ContentType`, `ows_FSObjType` (`0`=item, `1`=folder), `ows_FileRef`, `ows_FileLeafRef`, `ows_EncodedAbsUrl`, `ows_AttachmentUrls`.
- `ows_FSObjType` arrives as `1;#0` in some responses (lookup-style encoding). Handle both bare and prefixed forms.
- **`DateInUtc` is a claim, not a guarantee.** Before trusting datetimes, the operator must verify one known item against what the SharePoint UI shows. Add a `spconnect verify-time --list X --item N` command that prints the raw wire value, the decoded UTC value, and the item's display form URL, so a human can compare.

Every row must be emitted in **both** forms: the raw `ows_` attribute dict and the decoded dict. Storage is cheap; re-crawling a 20-year-old server is not. If the decoder turns out to be wrong about something, the raw capture means it can be fixed without touching the server again.

---

## 8. Schema model and lookup graph (`schema.py`)

Parse each list's `<Fields>` into `list[FieldDef]` pydantic models.

Then build the **lookup graph** across all crawled lists:

- **Nodes:** lists, keyed by GUID, carrying web URL, title, item count, base type.
- **Edges:** one per `Lookup`/`LookupMulti` field, from the containing list to the list named in the field's `List` attribute, carrying the field internal name, display name, `ShowField`, and whether it is multi-valued.

Resolve `List="Self"` to the containing list. A `List` GUID that matches no crawled list means the target is out of scope or inaccessible — record the edge as **dangling** rather than dropping it, and report the count.

Emit the graph as `landing/_graph.json` and also as `landing/_graph.mmd` (Mermaid `graph LR`) so a human can look at it. This graph is the recovered data model of the CRM and is the most valuable artifact of the whole crawl — the downstream pipeline uses it to decide which lists to denormalise into which documents.

Add `spconnect graph --format mermaid|json|dot`.

---

## 9. Landing zone contract (`landing.py`)

This layout is a **stable contract** consumed by the downstream pipeline. Do not change it without updating this spec.

```
landing/
├── _manifest.json          # crawl metadata: start/end time, server version,
│                           # config snapshot (password redacted), counts, errors
├── _state.json             # resumable checkpoints + per-list change tokens
├── _graph.json
├── _graph.mmd
├── webs.json               # flat inventory of all discovered webs
└── webs/
    └── {web_slug}/                       # slug = URL path, sanitised
        ├── web.json
        └── lists/
            └── {list_guid}/
                ├── list.json             # metadata + full field schema
                ├── items.jsonl           # one decoded item per line
                ├── items_raw.jsonl       # one raw ows_ dict per line
                └── files/
                    └── {item_id}/
                        └── {filename}
```

Every line in `items.jsonl`:

```json
{
  "doc_id": "{web_guid}:{list_guid}:{item_id}",
  "web_url": "http://sp/sites/service",
  "list_guid": "{...}",
  "list_title": "Servicefälle",
  "item_id": 4711,
  "display_url": "http://sp/sites/service/Lists/Cases/DispForm.aspx?ID=4711",
  "content_type": "Item",
  "created": "2009-03-14T08:11:00Z",
  "modified": "2011-07-02T15:43:00Z",
  "fields": { "...decoded, keyed by internal name..." },
  "field_display_names": { "internal": "Anzeigename" },
  "attachments": [
    {"filename": "foto.jpg", "url": "...", "local_path": "files/4711/foto.jpg",
     "bytes": 20481, "sha256": "...", "downloaded": true, "skip_reason": null}
  ]
}
```

`doc_id` must be **stable across runs** so the downstream pipeline can upsert idempotently. Never derive it from row position or crawl order.

`display_url` matters: the downstream RAG answers must cite back into SharePoint, because that is what will make people trust the new system.

Write JSONL incrementally with flush-per-line, not accumulated in memory. Some of these lists may have six figures of rows.

---

## 10. State and resumability (`state.py`)

`_state.json` holds, per list GUID:

```json
{
  "lists": {
    "{guid}": {
      "web_url": "...",
      "last_full_crawl": "2026-07-27T09:00:00Z",
      "last_item_id": 12800,
      "change_token": "1;3;{guid};638...;12345",
      "status": "complete|in_progress|failed",
      "error": null
    }
  }
}
```

Write it after every page, atomically (temp file + `os.replace`). A crawl interrupted at any point must resume from the last completed page with `spconnect crawl --resume`, without re-fetching completed work and without duplicating lines in the JSONL files (truncate any partial trailing content on resume).

---

## 11. CLI (`cli.py`)

```
spconnect probe                 # auth check + server version + one trivial call; exits nonzero on failure
spconnect discover              # webs + lists inventory only, no items. Fast. Run this first.
spconnect schema                # GetList for every in-scope list; writes list.json files
spconnect graph [--format ...]  # build/emit the lookup graph from cached schemas
spconnect crawl [--resume]      # full extraction
spconnect sync                  # incremental via change tokens
spconnect verify-time --list X --item N
spconnect stats                 # summarise the landing zone: lists, items, files, bytes, errors
```

Global flags: `--env-file`, `--log-level`, `--dry-run`, and scope overrides for the `SP_INCLUDE_*` / `SP_EXCLUDE_*` settings.

`--dry-run` must perform read-only discovery and print what *would* be crawled — list titles, item counts, estimated request count — without fetching items. The operator will use this to size the job before pointing it at production.

---

## 12. Logging and error policy

- Structured logging with a per-request context: web, list, operation, page, duration.
- Progress at INFO: `list 12/87 "Servicefälle" — 12,800/45,231 items`.
- **A failure in one list must not abort the crawl.** Catch, record into `_manifest.json` under `errors[]`, mark that list `failed` in state, continue to the next.
- Auth failures (401/403) are the exception: fail immediately and loudly. A misconfigured credential producing 87 "failed" lists is a bad experience.
- At the end, print a summary: lists succeeded/failed, items written, files downloaded, files skipped and why, dangling lookup edges, decoder warnings.

---

## 13. Testing

There is no live server available during development. **All tests must run offline against fixtures.**

### 13.1 Fixtures

Hand-write realistic XML fixtures under `tests/fixtures/`. They must include, deliberately:

- German umlauts and `ß` in titles and values, to catch encoding bugs.
- Internal names with `_x0020_` escaping.
- A multi-lookup with three values.
- A `MultiChoice` with the leading/trailing `;#` delimiters.
- A `Calculated` field with a `float;#` prefix and one with a `datetime;#` prefix.
- A `Note` field containing HTML with nested tags and an escaped `&`.
- A lookup pointing at a list GUID that is not in the fixture set (dangling edge).
- An empty list (zero rows).
- A page boundary: page 1 returns exactly `rowLimit` rows, page 2 returns fewer.
- A `soap:Fault` response.
- An item with attachments and one without.

### 13.2 Unit tests

- **`test_decode.py`** — the highest-value test file. Table-driven: every row of the decoding table in §7, plus edge cases (empty string, `None`, malformed lookup with odd token count, value containing `;#`). Assert both the decoded value and that malformed input produces a logged warning rather than an exception.
- **`test_schema.py`** — field parsing, `_xHHHH_` escape/unescape round-trip, lookup graph construction, `Self` resolution, dangling edge detection.
- **`test_soap.py`** — envelope construction (assert the exact `SOAPAction` header and namespace), XML-fragment parameters are real elements not escaped strings, fault parsing raises `SharePointSoapFault` with the right message.
- **`test_paging.py`** — ID-based paging loop: terminates correctly on a short page, terminates on an empty list, resumes from a non-zero `last_id`, does not loop forever if the server returns the same rows twice (guard: if `max(ID)` in a page is not greater than `last_id`, abort with an error).

### 13.3 Integration tests

Use `responses` to mock the HTTP layer and replay fixtures. Cover:

- `test_crawl.py` — full orchestration across two webs × three lists, asserting the landing zone comes out exactly as specified in §9.
- Resume behaviour: kill the crawl mid-list (raise from a mocked response), restart with `--resume`, assert no duplicate JSONL lines and no re-fetched pages.
- Incremental sync: a change token response with one update and one delete, asserting the delete is recorded.

### 13.4 Live smoke test

`test_live_smoke.py`, entirely skipped unless `SP_LIVE_TESTS=1`. When enabled it should: probe the version header, call `GetAllSubWebCollection`, call `GetListCollection` on the first web, and pull **exactly one page of one list**. It must be read-only and must not write to the landing zone. This is the operator's five-minute confidence check against the real server.

### 13.5 Coverage

Target ≥90% on `decode.py` and `schema.py`. Those two modules are where silent data corruption lives.

---

## 14. Implementation order

Build and verify in this sequence; each milestone should be independently runnable.

1. `config.py` + `.env.example` + transport with NTLM and the version probe → `spconnect probe` works.
2. `soap.py` + `webs.py` + `lists.py` (`GetListCollection`) → `spconnect discover` produces `webs.json`.
3. `schema.py` + `GetList` → `spconnect schema`, then `spconnect graph`. **Stop here and show the operator the Mermaid graph.** It is the first genuinely informative output and it will change decisions downstream.
4. `decode.py` with its full test suite, developed against fixtures **before** wiring it to the crawler.
5. `GetListItems` with ID-based paging + `landing.py` → `spconnect crawl` for a single list.
6. Full-crawl orchestration, state, resume.
7. `files.py` — attachments and document library downloads.
8. `GetListItemChangesSinceToken` → `spconnect sync`.
9. `stats`, `verify-time`, polish.

---

## 15. Things the implementation must NOT assume

Flag these clearly in the README as "verify on first contact with the real server". They are genuinely uncertain and the code should degrade gracefully rather than crash:

- **The exact SharePoint version.** Behaviour differs between WSS 2.0 and 3.0. The version probe drives this; do not hardcode.
- **That `DateInUtc` behaves as documented on this build.** Hence `verify-time`.
- **That authentication is NTLM.** Some installs of this era use Basic over HTTP, some use Kerberos, some use forms-based auth. Support NTLM and Basic; if the operator reports something else, that is a follow-up.
- **That `GetAllSubWebCollection` returns everything.** It returns what the credential can read. If the count looks low, the credential lacks permissions somewhere. Report the count prominently.
- **Item-level permissions.** If different technicians currently see different cases, this connector flattens that distinction, because it crawls as one identity. Print a prominent warning in the final summary if any list reports `HasUniqueScopes="True"` in `GetListCollection`, telling the operator that per-item security exists on that list and the downstream vector DB will not preserve it.

---

## 16. Acceptance criteria

- [ ] `spconnect probe` authenticates and reports the server build number.
- [ ] `spconnect discover` enumerates all webs and lists into `webs.json`.
- [ ] `spconnect schema` writes a complete field schema per list.
- [ ] `spconnect graph --format mermaid` emits a renderable graph of lookup relationships.
- [ ] `spconnect crawl` writes the §9 landing zone layout exactly.
- [ ] Every item appears in both `items.jsonl` (decoded) and `items_raw.jsonl` (raw).
- [ ] `doc_id` is stable across two consecutive full crawls of unchanged data.
- [ ] A crawl killed mid-run resumes with `--resume` producing no duplicates and no gaps.
- [ ] `spconnect sync` applies updates and records deletes.
- [ ] Attachments and document library files land on disk with sha256 and size recorded.
- [ ] Full test suite passes offline with no network access.
- [ ] `SP_PASSWORD` appears nowhere in logs, `_manifest.json`, or `repr(settings)`.
- [ ] `ruff check` and `mypy src/` are clean.
- [ ] README documents the landing zone format well enough that the downstream pipeline can be written against it without reading the connector source.
