# Troubleshooting

Symptom → cause → fix. Ordered roughly by how early you hit them.

**First move for anything unexplained:**

```bash
spconnect -vv probe 2> probe.log
```

`-vv` logs every request and response, and captures bodies to
`landing/_trace.log` (mode `0600`). The narrated steps tell you *which* part
failed; the trace tells you *why*.

---

## Connection and authentication

### `FAILED: ConnectionError` at step 1

The host is unreachable. Wrong `SP_BASE_URL`, firewall, DNS, or a proxy.

```bash
curl -sI http://sharepoint.intern.example.de/
```

If `curl` also fails, it is not the connector.

### `BaseUrlRedirectError` / `SoapRedirectError` (exit code 2)

`SP_BASE_URL` is not where the farm answers. The message names the value to
set; set it and re-run.

```
SP_BASE_URL http://crm.example.de answered HTTP 302 -> https://crm.example.de/Webs.asmx
  The server redirects http to https. Set SP_BASE_URL to https://crm.example.de …
```

The usual cause is a base URL on `http` while IIS redirects to `https`, or an
Alternate Access Mapping pointing at a different host.

**Why the connector refuses to just follow the redirect.** `requests` answers a
301/302/303 by rewriting `POST` to `GET` and discarding the body, so a
redirected SOAP call arrives at `*.asmx` as a bodyless GET and IIS returns the
service-description page with HTTP 200 — which is not SOAP, and reports as
[`no <…Result> element in response`](#no-getallsubwebcollectionresult-element-in-response)
several layers away from the actual problem. Following it would also make the
URLs recorded in the manifest, the web list and the state file disagree with
the zone the data came from.

**This one is easy to misread**, because GET-based checks keep working: the
version probe follows the redirect harmlessly and reports the build number, so
steps 1–4 pass and the farm looks reachable right up until the first real
operation. If `spconnect probe` prints a server version and then fails, suspect
the base URL before suspecting permissions.

Confirm it from the shell — a `Location:` header on a `_vti_bin` POST is proof:

```bash
curl -sS -o /dev/null -D - -X POST http://crm.example.de/_vti_bin/Webs.asmx \
  -H 'Content-Type: text/xml; charset=utf-8' \
  -H 'SOAPAction: "http://schemas.microsoft.com/sharepoint/soap/GetAllSubWebCollection"'
```

Note that some farms rewrite the *path* while redirecting
(`/_vti_bin/Webs.asmx` → `/Webs.asmx`). That target is not a URL to copy; the
connector's advice keeps your existing path and changes only the scheme and
host, which is what `SP_BASE_URL` wants.

### `FAILED: SSLError`

Only happens over **https**. Old IIS offers TLS 1.0 with ciphers modern OpenSSL
rejects outright.

```dotenv
SP_ALLOW_LEGACY_TLS=true
SP_VERIFY_SSL=false
```

A warning is logged whenever legacy TLS is active — that is expected, not a
problem.

### `AUTH FAILED` (exit code 2)

Step 2 tells you what the server actually wants:

```
[2/8] Determine authentication scheme .... OK  Negotiate, NTLM -> SP_AUTH_MODE=ntlm
```

| Server offers | Set |
|---|---|
| `Negotiate`, `NTLM` | `integrated` (best) or `ntlm` |
| `Basic` only | `basic` |
| Redirect to `login.aspx` | Forms auth — **not supported** |
| Nothing, HTTP 200 | `anonymous` |

Then check, in order:

1. **Double backslash.** `CONTOSO\\svc` in `.env` is read literally, with two
   backslashes. Write `CONTOSO\svc`.
2. **Missing domain.** NTLM usually wants `DOMAIN\username`. The probe says so
   if yours has no domain part.
3. **Account locked or password expired.** Test the same credential in a browser.
4. **The account cannot read that specific web** — 403 rather than 401.

### `SP_AUTH_MODE=integrated` needs a platform auth provider

No SSPI/Kerberos provider is installed. The error names the exact command:

```bash
pip install 'spconnect[windows]'      # domain-joined Windows
pip install 'spconnect[kerberos]'     # elsewhere, then kinit
```

Integrated auth uses the identity of the **running process**, so the crawl must
run *as* the service account — not as you, with the service account's name in a
config file.

---

## Responses that are not what we asked for

### `no <GetAllSubWebCollectionResult> element in response`

The server returned HTTP 200 but not the SOAP we expected. The error now
includes a diagnosis and the first 400 bytes, and the full body is written to
`landing/_last_bad_response.xml`.

| Diagnosis in the message | Cause |
|---|---|
| `forms-authentication login page` | Claims/FBA web app returning a sign-in page. **Not supported.** |
| `an HTML page … IIS error page, a proxy, or a login form` | Something between you and SharePoint is intercepting |
| `the server does not implement this operation` | Pre-WSS 3.0 build; the connector falls back automatically |
| `empty response body` | A proxy or WAF is stripping the response |
| `not a SOAP envelope at all` | `SP_BASE_URL` does not point at a SharePoint web |

### `SharePointSoapFault: … [0x82000006]`

An application error from SharePoint itself. `0x82000006` is "list does not
exist" — usually a stale cached schema. Delete the landing zone and re-run
`spconnect discover`.

---

## Scale and throttling

### A list fails with "Listenansichtsschwellenwert" / "list view threshold"

SharePoint 2010+ refuses queries that must examine more than 5000 rows. The
summary names these explicitly:

```
ERROR: these lists were throttled by the SharePoint 2010 list view threshold:
  - http://sp/sites/service :: Servicefälle
```

The connector already pages on the indexed `ID` column, which normally seeks the
index and stays under the limit. If a list still trips it, the fix is on the
server side:

- ask the farm admin to raise or disable the threshold,
- add an index to the column being filtered,
- or run the crawl inside the **daily unthrottled window** the farm defines.

Lowering `SP_PAGE_SIZE` does **not** help — the limit is about rows examined,
not rows returned.

### "over 5000 items: N list(s)" but the crawl succeeded

Informational. Those lists exceed the threshold but crawled fine, because ID
paging seeks the index. No action needed.

### The crawl is very slow

By design. `SP_REQUESTS_PER_SECOND` (default `3`) throttles everything.
`spconnect crawl --dry-run` estimates wall-clock up front. Raise the rate only
if the farm can take it — it is twenty years old.

---

## Data problems

### Datetimes look wrong by a few hours

`DateInUtc` is not behaving as documented on this build. Confirm:

```bash
spconnect verify-time --list "Servicefälle" --item 4711
```

Open the printed display-form URL and compare. If they disagree, every datetime
is offset by the server's UTC offset — **recoverable from `items_raw.jsonl`
offline**, no re-crawl needed. Report it and it can be corrected in the decoder.

### A lookup shows the wrong customer name

Expected, and not a bug. The display half of a lookup is a denormalised snapshot
written when the row was last saved. Join on `id`, never on `value`. See
[landing-zone.md §4](landing-zone.md#4-fields).

### A column is missing from `fields`

- `ows_MetaInfo` is skipped deliberately.
- Otherwise, check `list.json` — if the column is absent there too, the
  credential may not be able to see it, or it is a view-only computed column.
- Under `SP_API_MODE=odata`, an unmappable property is kept under its OData name
  rather than dropped. Look for it there.

### "decoder warnings: N" in the summary

A value did not match its declared type. Warnings name the list, item ID and
field. The raw value is preserved in `items_raw.jsonl`, so nothing is lost.
Common cause: a display value containing a literal `;#`.

### Dangling lookup edges in the graph

A lookup points at a list that was not crawled — out of scope, or unreadable by
this credential. The edge is kept and marked `dangling: true` deliberately.
If you need the target, widen `SP_INCLUDE_LISTS` or check permissions.

---

## Crawl behaviour

### One list failed but the crawl continued

Correct behaviour. Per-list isolation is deliberate — one bad list must not cost
you the other 86. Details are in `_manifest.json` under `errors[]`, and the list
is marked `failed` in `_state.json`.

```bash
spconnect crawl --resume     # retries failed lists, skips completed ones
```

### `paging stalled on '…'`

The server returned a page whose maximum ID was not greater than the previous
checkpoint — it is ignoring the `ID > n` filter. The connector aborts that list
rather than looping forever. This is a server-side anomaly; capture it with
`-vv` and report it.

### Everything failed with the same auth error

Authentication failures abort the whole run immediately, by design. A
misconfigured credential producing 87 "failed" lists is a useless artifact.

### Resume produced duplicates

It should not, and there are tests asserting it does not. If you see this,
capture `_state.json` and the first duplicated line and report it. Workaround:
delete that list's directory and re-crawl it with `--include-lists`.

---

## REST backend (`SP_API_MODE=odata`)

### `ListData.svc: unavailable (404)`

The OData feature is not enabled on that web, or the endpoint does not exist.
Stay on `SP_API_MODE=soap` — it is the default and works on every build from
2003 to 2016.

### `NO MATCH` in the probe's entity-set mapping

```
[8/8] ListData.svc (REST backend) ... OK  available
      !! 'Fälle 2008' -> NO MATCH
```

`ListData.svc` derives entity-set names from list titles: spaces stripped, words
capitalised, non-ASCII mangled. Lists it cannot match **fall back to SOAP
automatically** and are reported in the summary — nothing is skipped.

Known hard failure: `$metadata` breaks outright if a list title starts with a
digit.

### Values differ between backends

They should not; there are equivalence tests. If you find a divergence, that is
a genuine bug worth reporting — run both into separate landing dirs and diff:

```bash
spconnect crawl --api-mode soap  --landing-dir ./landing-soap --include-lists "X"
spconnect crawl --api-mode odata --landing-dir ./landing-rest --include-lists "X"
```

---

## Still stuck?

Collect and send:

1. `spconnect -vv probe 2> probe.log` — the log
2. `landing/_trace.log` — request/response bodies (**check it first**; it may
   contain business data, though the password is scrubbed)
3. `landing/_last_bad_response.xml` if it exists
4. `landing/_manifest.json`

None of these contain `SP_PASSWORD` — see [security.md](security.md) — but
`_trace.log` can contain list content, so review before sharing.
