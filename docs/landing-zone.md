# The landing zone contract

**Audience:** whoever writes the downstream chunking / embedding / vector-DB
pipeline.

This document is the interface. You should be able to write the whole
downstream pipeline against it without reading a line of connector source. If
you find yourself having to, that is a documentation bug — please report it.

**This layout is a contract.** It does not change without a version bump and a
note in [decisions.md](decisions.md).

---

## 1. Directory layout

```
landing/
├── _manifest.json           # what this run did, and what went wrong
├── _state.json              # resume checkpoints + per-list change tokens
├── _graph.json              # the lookup graph, machine-readable
├── _graph.mmd               # the same graph as Mermaid
├── _graph.dot               # only if `spconnect graph --format dot` was run
├── _trace.log               # only if SP_LOG_BODIES=true. Mode 0600. Not data.
├── _last_bad_response.xml   # only after a parse failure. Mode 0600. Not data.
├── webs.json                # flat inventory of every discovered web
└── webs/
    └── {web_slug}/                    # host + URL path, unsafe chars -> "_"
        ├── web.json
        └── lists/
            └── {list_guid}/           # lowercase, braces stripped
                ├── list.json          # list metadata + full field schema
                ├── items.jsonl        # one decoded item per line
                ├── items_raw.jsonl    # one raw item per line, same order
                └── files/
                    └── {item_id}/
                        └── {filename}
```

Everything prefixed `_` is run metadata, not content. **Ignore `_trace.log` and
`_last_bad_response.xml` entirely** — they are diagnostic captures, they may
contain response headers, and they are deliberately mode `0600`.

### Finding the content

```python
from pathlib import Path

for list_json in Path("landing").glob("webs/*/lists/*/list.json"):
    items = list_json.parent / "items.jsonl"
```

Do not glob for `*.jsonl` from the root — you would pick up `items_raw.jsonl`
as well and double-count every document.

---

## 2. `items.jsonl` — the documents

One JSON object per line, UTF-8, `\n`-terminated, flushed per line. Written
incrementally: a crawl killed halfway leaves a valid prefix, never a truncated
object (see [§7](#7-guarantees)).

```json
{
  "doc_id": "dffdb785-4152-5ed5-938c-ad8665fbb36e:{11111111-1111-1111-1111-111111111111}:4711",
  "web_url": "http://sp/sites/service",
  "web_id": "dffdb785-4152-5ed5-938c-ad8665fbb36e",
  "list_guid": "{11111111-1111-1111-1111-111111111111}",
  "list_title": "Servicefälle",
  "item_id": 4711,
  "display_url": "http://sp/sites/service/Lists/Cases/DispForm.aspx?ID=4711",
  "content_type": "Element",
  "created": "2009-03-14T08:11:00Z",
  "modified": "2011-07-02T15:43:00Z",
  "is_folder": false,
  "file_ref": "sites/service/Lists/Cases/4711_.000",
  "file_name": "4711_.000",
  "fields": { "…" },
  "field_display_names": { "…" },
  "attachments": [ "…" ]
}
```

| Key | Type | Notes |
|---|---|---|
| `doc_id` | string | **Upsert key.** Stable across runs. See [§3](#3-docid). |
| `web_url` | string | Normalised: lowercase host, no trailing slash, path case preserved |
| `web_id` | string | Synthetic but deterministic — see [§3](#3-docid) |
| `list_guid` | string | Braced, uppercase hex, e.g. `{A1B2…}` |
| `list_title` | string | As shown in SharePoint. Usually German. |
| `item_id` | int | SharePoint list item ID. Unique **within a list**, not globally. |
| `display_url` | string | Deep link to the item's display form. **Cite this.** |
| `content_type` | string \| null | e.g. `Element`, `Dokument`, `Ordner` |
| `created` / `modified` | string \| null | `YYYY-MM-DDTHH:MM:SSZ`, UTC — but read [§6](#6-datetimes) |
| `is_folder` | bool | `true` for document-library folders. **Usually skip these.** |
| `file_ref` | string \| null | Server-relative path |
| `file_name` | string \| null | Leaf name |
| `fields` | object | The data. Keys are internal names — see [§4](#4-fields) |
| `field_display_names` | object | internal name → German label users recognise |
| `attachments` | array | See [§5](#5-attachments) |

---

## 3. doc_id

```
{web_id}:{list_guid}:{item_id}
```

**Upsert on this.** It is stable across runs and never derived from row position
or crawl order. Two consecutive full crawls of unchanged data produce byte-identical
`doc_id`s, and both the SOAP and REST backends produce the same ones — there are
regression tests for both.

`web_id` is `uuid5(NAMESPACE_URL, normalised_web_url)`. It is **synthetic**: none
of the SOAP operations this connector is permitted to use returns a real web
GUID, so a deterministic hash of the URL stands in for one.

> **The one way `doc_id` can change:** if the farm moves to a new hostname or the
> site is relocated, `web_id` changes and every document gets a new `doc_id`.
> Plan a re-index for that, or map old→new before upserting.

---

## 4. fields

Keys are **internal names, still `_xHHHH_`-escaped**. `Case Number` appears as
`Case_x0020_Number`. This is deliberate: internal names are the stable join key,
whereas display names get renamed by users. Use `field_display_names` when you
need something human-readable.

To unescape: `_xHHHH_` → the character with that hex codepoint. `_x0020_` is a
space, `_x00e4_` is `ä`.

### Value shapes

| SharePoint type | JSON | Example |
|---|---|---|
| `Text`, `Note` | string | `"Getriebeschaden"` (Note may contain HTML) |
| `Number`, `Currency` | number | `1234.5` |
| `Counter`, `Integer` | integer | `42` |
| `Boolean` | bool | `true` |
| `DateTime` | string | `"2009-03-14T08:11:00Z"` |
| `Choice` | string | `"Hoch"` |
| `MultiChoice` | array of string | `["Reparatur","Garantie"]` |
| `Lookup` | object | `{"id":42,"value":"Müller GmbH"}` |
| `LookupMulti` | array of object | `[{"id":42,…},{"id":57,…}]` |
| `User` | object | `{"id":12,"value":"CONTOSO\\jdoe"}` |
| `UserMulti` | array of object | as above |
| `URL` | object | `{"url":"http://…","description":"Anzeigetext"}` |
| `Calculated` | depends | Type prefix stripped, then decoded per that type |
| `Attachments` | bool | `true` if the item has attachments |
| `File` | object | `{"id":1,"value":"Handbuch.pdf"}` |
| `TaxonomyFieldType` † | object | `{"id":3,"value":"Reparatur","term_guid":"9f8e…"}` |
| `TaxonomyFieldTypeMulti` † | array of object | as above |
| `RatingCount` † | integer | `12` |
| `AverageRating` † | number | `3.5` |

† SharePoint 2010+ only. `TaxonomyFieldType` is Managed Metadata.

Any type not listed comes through as its **raw string**. It is never dropped and
never silently coerced.

### Three rules that will bite you

1. **Join on `id`, not on `value`.** The display string in a lookup is a
   denormalised snapshot written when the row was last saved. If someone renamed
   the customer in 2014, rows saved before then still carry the old name. The
   `id` points at the current row.
2. **For taxonomy fields, join on `term_guid`.** Labels are language-specific and
   get renamed; term GUIDs do not.
3. **`ows_MetaInfo` is absent.** It is a property bag with its own internal
   format and is skipped entirely, in both files.

### Useful system fields

Present in `fields` alongside the business columns: `ID`, `UniqueId`, `GUID`,
`Created`, `Modified`, `Author`, `Editor`, `ContentType`, `FSObjType`
(`0`=item, `1`=folder), `FileRef`, `FileLeafRef`, `EncodedAbsUrl`,
`AttachmentUrls`, `Attachments`.

---

## 5. `attachments`

```json
[{
  "filename": "foto.jpg",
  "url": "http://sp/sites/service/Lists/Cases/Attachments/4711/foto.jpg",
  "local_path": "files/4711/foto.jpg",
  "bytes": 20481,
  "sha256": "9ed3fded…",
  "downloaded": true,
  "skip_reason": null
}]
```

`local_path` is **relative to the list directory** — the one containing
`items.jsonl`. Resolve it as `list_dir / local_path`.

When `downloaded` is `false`, `local_path`, `bytes` and `sha256` are `null` and
`skip_reason` says why:

| `skip_reason` | Meaning |
|---|---|
| `downloads_disabled` | `SP_DOWNLOAD_FILES=false` for that run |
| `extension_excluded:.exe` | Matched `SP_SKIP_EXTENSIONS` |
| `too_large:N>M` | Exceeded `SP_MAX_FILE_MB` |
| `download_failed:NotFoundError` | The fetch failed; the exception type follows the colon |

For **document libraries** the item *is* a file: its single "attachment" entry is
the document itself, from `ows_EncodedAbsUrl`. Folder rows (`is_folder: true`)
have an empty `attachments` array — they have no bytes.

---

## 6. Datetimes

All datetimes are emitted as `YYYY-MM-DDTHH:MM:SSZ` and are **intended** to be
UTC. The connector requests `DateInUtc=TRUE` from SharePoint.

> **Verify this once before trusting it at scale.** `DateInUtc` is a claim about
> the server's behaviour, not a guarantee, and this connector has not been able
> to confirm it against the real farm. Run:
>
> ```bash
> spconnect verify-time --list "Servicefälle" --item 4711
> ```
>
> It prints the raw wire value, the decoded UTC value, and the display-form URL.
> Open the URL, compare. If they disagree, every datetime in the landing zone is
> off by the server's UTC offset — recoverable from `items_raw.jsonl` without
> re-crawling, but you want to know before indexing 45,000 cases.

---

## 7. `items_raw.jsonl`

Same rows, same order, one line each, holding the values **exactly as the server
sent them** — `ows_`-prefixed strings under SOAP, the OData entity under REST
(minus `__metadata`/`__deferred` control keys).

This exists so that a decoder bug is recoverable **without touching the server
again**. Re-crawling a twenty-year-old farm is expensive; disk is not. If a
field turns out to be decoded wrongly, the fix can be applied offline against
this file.

You do not need it for normal ingestion. Keep it anyway.

---

## 8. `_graph.json` — the recovered data model

The lookup graph is the most valuable artifact of the crawl: it is the CRM's
implicit foreign-key schema, which nobody documented.

```json
{
  "nodes": [{"list_guid": "{…}", "title": "Servicefälle", "web_url": "…",
             "item_count": 45231, "base_type": "0", "base_type_name": "generic_list"}],
  "edges": [{"source_list_guid": "{…}", "target_list_guid": "{…}",
             "source_list_title": "Servicefälle", "target_list_title": "Kunden",
             "field_name": "Kunde", "field_display_name": "Kunde",
             "show_field": "Title", "multi": false,
             "self_reference": false, "dangling": false}]
}
```

- One **node** per crawled list.
- One **edge** per `Lookup`/`LookupMulti` column: source list → target list.
- `field_name` is the key in `fields` carrying the foreign key.
- `self_reference: true` — the column points at its own list (`List="Self"`).
- `dangling: true` — the target list was **not crawled**: out of scope, or the
  credential cannot read it. The edge is kept rather than dropped, because
  knowing a foreign key leaves your dataset is information.

**Use this to decide what to denormalise.** A service case whose `Kunde` lookup
is resolved into the document text is far more retrievable than one that says
`{"id": 42}`. The graph tells you which joins exist and which are worth
following.

`_graph.mmd` is the same graph as Mermaid — paste it into any viewer to see the
model at a glance. Document libraries are drawn as cylinders, dangling edges as
dashed lines.

---

## 9. `_manifest.json` — did this run actually work?

**Check this before ingesting.** A crawl that reports success in the shell can
still have failed on individual lists — by design, one bad list does not abort
the run.

| Key | Why you care |
|---|---|
| `command`, `started_at`, `finished_at` | Which run produced this |
| `counts.lists_failed` | **If non-zero, some lists are missing or partial** |
| `counts.items_written` | Sanity-check against your ingested count |
| `errors[]` | Per-failure: scope, web, list, operation, error type, message |
| `warnings[]` | Threshold hits, REST fallbacks, invalid change tokens |
| `lists_with_unique_scopes` | **Item-level permissions — see below** |
| `server_version` | Build number and derived capabilities |
| `api_mode` | `soap` or `odata` — which backend produced the items |
| `web_discovery_method` | `GetAllSubWebCollection` or the `GetWebCollection` walk |
| `config` | Full settings snapshot, password redacted |

### The permissions warning

Any list named in `lists_with_unique_scopes` has **item-level security** in
SharePoint. This connector crawls as a single identity, so that distinction is
flattened: every item lands in the same place with no ACL.

If technicians currently see different subsets of cases, and you index this
without further work, **the vector DB will surface all of it to everyone.**
That is a product decision, not a technical detail — escalate it rather than
absorbing it.

---

## 10. `_state.json`

Resume checkpoints. Not needed by the downstream pipeline, but useful for
diagnosis: `status` per list is `pending` / `in_progress` / `complete` /
`failed`, alongside `last_item_id`, `items_written`, `change_token` and the last
`error`.

A list showing `failed` here is a list whose `items.jsonl` is incomplete.

---

## 11. Guarantees

What the connector promises:

- **`doc_id` is stable** across runs, across backends, and across resume.
- **No duplicate lines.** Resume truncates any rows past the last checkpoint,
  including a half-written trailing line from a killed process.
- **No partial JSON objects.** Lines are written and flushed whole.
- **Every item appears in both** `items.jsonl` and `items_raw.jsonl`.
- **`SP_PASSWORD` appears nowhere** in the landing zone, including
  `_manifest.json`.
- **Read-only.** The connector never writes to SharePoint.

What it does *not* promise:

- That every list succeeded — **check `_manifest.json`**.
- That item-level permissions are preserved — they are not.
- That `created`/`modified` are true UTC until `verify-time` has been run once.
- That version history exists — only current versions are extracted, by design.

---

## 12. Minimal reader

```python
import json
from pathlib import Path


def read_landing_zone(root=Path("landing")):

    manifest = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))
    if manifest["counts"]["lists_failed"]:
        raise SystemExit(f"{manifest['counts']['lists_failed']} lists failed — see errors[]")

    graph = json.loads((root / "_graph.json").read_text(encoding="utf-8"))
    lookup_targets = {(e["source_list_guid"], e["field_name"]): e["target_list_guid"] for e in graph["edges"]}

    for list_json in sorted(root.glob("webs/*/lists/*/list.json")):
        list_dir = list_json.parent
        items = list_dir / "items.jsonl"
        if not items.exists():
            continue

        with items.open(encoding="utf-8") as handle:
            for line in handle:
                item = json.loads(line)
                if item["is_folder"]:
                    continue  # folders carry no content

                text_parts = [
                    f"{item['field_display_names'].get(k, k)}: {v}"
                    for k, v in item["fields"].items()
                    if v not in (None, "", [])
                ]

                yield {
                    "id": item["doc_id"],  # upsert key
                    "text": "\n".join(str(p) for p in text_parts),
                    "source_url": item["display_url"],  # cite this
                    "list": item["list_title"],
                    "modified": item["modified"],
                    "lookup_targets": lookup_targets,  # resolve ids -> other lists
                    "files": [list_dir / a["local_path"] for a in item["attachments"] if a["downloaded"]],
                }
```

---

## See also

- [operations.md](operations.md) — running the connector
- [architecture.md](architecture.md) — how it works internally
- [decisions.md](decisions.md) — why it works that way
