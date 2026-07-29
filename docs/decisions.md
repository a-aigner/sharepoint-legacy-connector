# Decision record

Why this works the way it does — including the things that turned out to be
wrong, because a decision log that only records successes is not much use.

---

## D1 — ASMX SOAP, not REST or CSOM

**Status:** accepted. Revisit if the farm moves past 2019.

The original spec assumed WSS 3.0 / MOSS 2007, where ASMX under `_vti_bin/` is
genuinely the only remote interface. The farm turned out to be **SharePoint
2010**, which also ships `ListData.svc` (OData v2) and `client.svc` (CSOM), so
the premise no longer strictly holds.

We stayed on ASMX anyway:

- It is implemented, tested, and behaves identically from 2003 to 2016.
- OData cannot do web discovery, cannot give list GUIDs or internal field names,
  and has **no change feed** — so no deletes.
- The one thing REST would genuinely improve, value decoding, was already
  finished and tested at 95%.

Accounting done at the time: a rewrite would delete ~335 lines and add ~350,
while leaving 66% of the codebase untouched and requiring **two** protocol
stacks rather than one, since SOAP is still needed for discovery and deletes.

**Not** rejected for effort. Rejected because it was not a replacement.

## D2 — REST as a second backend, not a replacement

**Status:** accepted, off by default.

`SP_API_MODE=odata` switches the **item fetch** to `ListData.svc`, and nothing
else. Chosen over a rewrite so the decision stays reversible and the two
backends can be compared on real data rather than on argument.

That comparison immediately earned its keep: OData v2 serialises `Edm.Decimal`
as a *string*, so currency values differed between backends. Caught on the first
side-by-side run, fixed by coercing through the schema.

Known REST caveats, all documented and handled: entity-set names are derived
from list titles with non-ASCII mangled (so we read the service document rather
than guessing); `$metadata` breaks if a title starts with a digit; wide
`$expand` can 500; the 5000-item threshold applies identically.

## D3 — ID paging, not token paging

**Status:** accepted.

`ID > last_id ORDER BY ID` rather than `ListItemCollectionPositionNext`. The
token is not resumable across process restarts and needs re-escaping.

Chosen for resumability. **Turned out to also be the standard mitigation for the
SharePoint 2010 list view threshold**, because `ID` is always indexed, so the
query seeks rather than scans. A correctness decision that paid an unrelated
dividend.

## D4 — `doc_id` from a synthetic `web_id`

**Status:** accepted, with a known limitation.

The contract calls for `{web_guid}:{list_guid}:{item_id}`, but none of the seven
permitted SOAP operations returns a web GUID. `web_id` is therefore
`uuid5(NAMESPACE_URL, normalised_web_url)` — deterministic, stable across runs
and backends, no extra request.

**Limitation:** if the farm moves hostname, every `doc_id` changes. Documented
in [landing-zone.md](landing-zone.md#3-docid).

## D5 — Every row stored twice

**Status:** accepted.

Both `items.jsonl` (decoded) and `items_raw.jsonl` (as received). Roughly doubles
the storage.

Justified by asymmetry: disk is cheap, re-crawling a twenty-year-old production
farm is not. A decoder bug found after the crawl is fixable offline instead of
requiring another pass.

## D6 — Explicit `<ViewFields>`

**Status:** accepted.

With an empty `viewName` *and* empty `viewFields`, SharePoint returns the
**default view's** columns — not all of them. That is a silent way to lose entire
columns. The crawler always sends an explicit `<ViewFields>` built from the
list's own schema.

## D7 — Per-list error isolation, but fatal auth failures

**Status:** accepted.

One failing list is recorded in the manifest and the crawl continues. Auth
failures abort immediately.

The asymmetry is deliberate: a misconfigured credential producing 87 "failed"
lists is a useless artifact that looks like a data problem.

## D8 — Lists crawled sequentially

**Status:** accepted.

`SP_CONCURRENCY` parallelises **file downloads within an item**, not lists.
Since the global rate limiter caps total throughput anyway, list-level
parallelism would buy little while making progress logging and checkpointing
non-deterministic.

## D9 — Integrated auth as the recommended mode

**Status:** accepted.

Added after a leak review. `SP_AUTH_MODE=integrated` authenticates as the
process's own identity via Windows SSPI or Kerberos, so no password is read,
stored, or held in memory.

Scrubbing is a blocklist over an infinite space of encodings — a mitigation.
Not having the secret is a control. Both are shipped; only one is recommended.

The built-in default remains `ntlm` because it needs no extra packages;
`.env.example` ships `integrated`.

## D10 — Bodies to a private file, never the log stream

**Status:** accepted.

`-vv` writes request/response bodies to `landing/_trace.log` at mode `0600`,
applied at `open()` time so there is no world-readable window. stderr carries
only a sequence number and the path.

stderr is the stream most likely to be redirected into a shared file, piped to a
collector, or pasted into a ticket.

## D11 — Header redaction by allowlist

**Status:** accepted, replaced an earlier denylist.

Anything not explicitly known-safe is redacted. A denylist cannot cover a header
nobody thought of — a reverse proxy's `X-Forwarded-Authorization`, a vendor
token — and the failure mode of guessing wrong is a credential in a log file.

`WWW-Authenticate` is special-cased down to its scheme names, since the value can
carry a Negotiate/GSSAPI token but the schemes are the diagnostic part.

---

## Things that were wrong

### The WSS 2.0 hypothesis

When `GetAllSubWebCollection` failed, the leading theory was that the farm
predated WSS 3.0, where that operation does not exist. A `GetWebCollection`
fallback was built for it.

**The farm is SharePoint 2010**, where the operation definitely exists. The
hypothesis was wrong.

The fallback was kept: it also triggers on a fault or non-SOAP body, so it costs
nothing and covers a version header that lies. The genuinely useful half of that
work was the *diagnostic* — `SoapResponseError` now carries the response body and
a plain-language cause.

**Lesson:** the diagnostic was worth more than the fix, because it was not
contingent on the hypothesis being right.

### Scrubbing that did not scrub

The first secret-scrubbing implementation was reviewed under the assumption it
worked. Testing it found **three of four leak paths open**: nested dicts were
untouched, rendered tracebacks bypassed it entirely (the processor ran *before*
`format_exc_info`), and `base64("user:pass")` contains no verbatim password at
all.

Writing the fix surfaced a fourth: base64 aligns on 3-byte boundaries, so
`b64("pass")` can be a literal substring of `b64("user:pass")`. Replacing the
short secret first *fragmented* the long one and left partial credential
material behind. Scrubbing now runs longest-first.

**Lesson:** a security control that has not been tested is a security claim.

### The raw-file truncation bug

`items_raw.jsonl` is written with `ows_`-stripped keys, but resume-truncation
matched on `ows_ID`. It matched nothing, so every raw line looked "past the
checkpoint" and the entire raw file was deleted on `--resume` — destroying
exactly the safety net D5 exists to provide.

Found by the first test written against it.

### `DefaultViewUrl` contains "fault"

The "is this 500 a SOAP fault?" check did a substring search for `fault`. That
matches `DefaultViewUrl`, an attribute on every `<List>` element the farm
returns — so genuine transient 500s would never have been retried. Now a regex.

---

## Open questions

Carried from the spec's "verify on first contact", still unresolved:

1. **Does `DateInUtc` behave as documented on this build?** `verify-time` exists
   to settle it. Until then, every datetime is a claim.
2. **What is actually intercepting the SOAP responses?** The current blocker.
   Claims-mode FBA is the leading guess; `-vv` plus `_last_bad_response.xml`
   will confirm.
3. **Are there Managed Metadata columns?** The taxonomy decoder is written from
   documentation and has never seen a real value from this farm.
4. **Does the credential see everything?** `GetAllSubWebCollection` returns only
   what it can read. The web count in `probe` is the only signal.
5. **Do the German list titles survive `ListData.svc`'s sanitiser?** Only
   relevant if REST is used; `probe` prints the mapping.
