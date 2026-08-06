# Operations runbook

**Audience:** whoever runs the extraction against the real farm.

---

## 1. Install

Python **3.11 or newer** is required.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pip install -e ".[windows]"     # domain-joined Windows — see §2
```

### Air-gapped install

Build a wheelhouse on a connected machine, carry it across:

```bash
# connected machine
pip download -d wheels ".[dev]" ".[windows]"

# secure machine
pip install --no-index --find-links wheels -e ".[dev]" -e ".[windows]"
```

### Verify the install before touching the server

```bash
pytest
```

Roughly 490 tests, entirely offline, no network. If they pass, the clone is
intact and every failure from here on is about the server, not the code.

---

## 2. Configure

```bash
cp .env.example .env
```

`.env` is gitignored. Do not commit it, and prefer not to create it at all —
see the recommended auth mode below.

### The only settings you must decide

```dotenv
SP_BASE_URL=http://sharepoint.intern.example.de   # no trailing slash needed
SP_AUTH_MODE=integrated                            # strongly recommended
```

**`integrated` needs no username and no password.** It authenticates as the
identity the process is already running under, so nothing sensitive is read,
stored, or held in memory. Run the crawl *as* the service account (scheduled
task, `runas`, or simply logged in as it).

If you cannot use integrated auth, fall back to:

```dotenv
SP_AUTH_MODE=ntlm
SP_USERNAME=CONTOSO\svc_extract     # ONE backslash — do not double it
SP_PASSWORD=…
```

> **`.env` parsing traps, all verified:**
> - `CONTOSO\svc_extract` — correct. `CONTOSO\\svc_extract` is read **literally,
>   with two backslashes**, and auth fails.
> - `#` and `$` in a password are safe unquoted.
> - Leading/trailing spaces are stripped — quote the value if it has them.
> - A trailing slash on `SP_BASE_URL` is stripped automatically.

Note the built-in default for `SP_AUTH_MODE` is `ntlm` (it needs no extra
packages); `.env.example` ships `integrated` because that is the recommendation.

### Everything else has a working default

Full reference: [configuration.md](configuration.md).

Worth knowing on day one:

| Setting | Default | When to change it |
|---|---|---|
| `SP_REQUESTS_PER_SECOND` | `3` | Lower it if the farm is struggling. This governs *everything*, downloads included. |
| `SP_PAGE_SIZE` | `200` | Lower for a slow server; higher makes each request heavier |
| `SP_MAX_FILE_MB` | `100` | Raise if the library holds large CAD/PDF files you need |
| `SP_ALLOW_LEGACY_TLS` | `true` | Only matters over **https**. Plain HTTP ignores it. |
| `SP_INCLUDE_LISTS` | *(all)* | Scope a first run to one list |

---

## 3. The escalation ladder

Run these in order. Each rung proves something the next depends on, so a failure
tells you exactly where you are.

### Rung 1 — `spconnect probe`

```bash
spconnect probe
```

Eight narrated steps. Each names what it is about to try *before* trying it, so
a hang is attributable:

```
[3/8] Authenticate as the current process identity .. OK       0.31s  login successful
[4/8] Read server build number ..................... OK       0.04s  14.0.4762.1000
      product: SharePoint 2010
      5000-item list view threshold applies on this build.
[5/8] Enumerate webs ............................... OK       0.61s  2 readable via GetAllSubWebCollection
```

Exit codes: `0` fine, `2` authentication, `1` everything else.

**Read the web count.** It reflects what *this credential* can see, not what
exists. A low number means missing permissions, and lists you never see will
silently never reach the index.

### Rung 2 — live smoke test

```bash
SP_LIVE_TESTS=1 pytest tests/test_live_smoke.py -v -s
```

Read-only, writes nothing to the landing zone, pulls exactly one page of one
list. Prints raw datetimes so you get an early look at the `DateInUtc` question.

### Rung 3 — `spconnect discover`

```bash
spconnect discover
```

Webs and lists inventory, no items. Fast. Writes `webs.json`. This is where
you find out how big the job is and which lists carry item-level permissions.

### Rung 4 — `spconnect schema` and `graph`

```bash
spconnect schema
spconnect graph --format mermaid > graph.mmd
```

**Stop and look at the graph.** It is the recovered data model of the CRM and
the first genuinely informative output — it will change downstream decisions
about what to denormalise. Paste it into any Mermaid viewer.

### Rung 5 — size the job

```bash
spconnect crawl --dry-run
```

Prints per-list item counts, page counts, estimated request count and estimated
wall-clock at your configured rate limit. Fetches **no items**. Use this before
pointing anything at production.

### Rung 6 — verify time

```bash
spconnect verify-time --list "Servicefälle" --item 4711
```

Prints raw wire value, decoded UTC value, and the display-form URL. Open the
URL and compare. This settles whether `DateInUtc` behaves as documented on this
build — do it once, before indexing at scale.

### Rung 7 — one list, for real

```bash
spconnect crawl --include-lists "Servicefälle"
spconnect stats
```

Inspect `landing/` by hand. Confirm the shape matches
[landing-zone.md](landing-zone.md).

### Rung 8 — the full crawl

```bash
spconnect crawl
```

Interrupt-safe. Resume with `spconnect crawl --resume`.

---

## 4. Commands

| Command | What it does |
|---|---|
| `spconnect probe` | Eight-step connectivity and capability check. Nonzero exit on failure. |
| `spconnect permissions [--json] [--no-probe-items]` | What this credential can actually read, web by web and list by list |
| `spconnect discover` | Webs + lists inventory. No items. |
| `spconnect schema` | `GetList` per in-scope list; writes `list.json`, rebuilds the graph |
| `spconnect graph --format mermaid\|json\|dot [--out FILE]` | Emit the lookup graph from cached schemas |
| `spconnect crawl [--resume]` | Full extraction |
| `spconnect sync` | Incremental update via change tokens, including deletes |
| `spconnect verify-time --list X --item N` | Raw vs decoded datetime vs display URL |
| `spconnect stats` | Summarise the landing zone on disk |

### Checking what the account may read

`spconnect permissions` answers "does this user have the right permissions" by
**trying**, not by asking. Enumerating a principal's permissions is itself a
privileged operation that a read-only service account usually cannot perform,
so the command reports two things and trusts the second:

* **Declared** — the groups and permission levels `UserGroup.asmx` will admit
  to. Often `not permitted to say`, which is expected and not a problem.
* **Effective** — one list-collection call per web and one single-row read per
  list. This needs no privilege beyond what the crawl needs anyway, and is the
  answer that matters: a role assignment overridden by broken inheritance
  further down is not a permission the crawl can use.

```
http://sp/sites/service  (5/6 lists readable)
  ok       12,481  Servicefälle  [unique-scopes]
  DENIED        0  Personalakten
           Zugriff verweigert.

1 web(s) and 1 list(s) are not readable. A crawl would silently omit them —
grant Read, or scope them out explicitly.
```

Run it after `probe` and before the first real crawl: an under-permissioned
credential does not fail a crawl, it quietly produces a smaller one.

`[unique-scopes]` means item-level permissions, where *readable* still does not
mean *complete* — rows the account cannot see are simply absent, and no
permission table above item level will reveal that.

### Global flags

`--env-file`, `--base-url`, `--landing-dir`, `--api-mode`, `--page-size`,
`--include-webs`, `--exclude-webs`, `--include-lists`, `--exclude-lists`,
`--include-hidden-lists`, `--include-document-libraries`, `--download-files`,
`--dry-run`, `--log-level`, `--log-format`, `-v` / `-vv`, `-q`, `--version`.

Precedence: **CLI flag > environment variable > `.env` > built-in default**.

---

## 5. Resuming, and what "safe to interrupt" means

State is written after **every page**, atomically. Ctrl-C at any point is safe.

```bash
spconnect crawl --resume
```

Resume skips lists already marked `complete`, truncates any rows past the last
checkpoint — including a half-written trailing line from a killed process — and
continues from `last_item_id`. No duplicates, no gaps. There are tests for
exactly this.

Without `--resume`, a crawl starts each list clean, replacing its previous
output rather than appending.

To retry only the failed lists after a partial run: `--resume` does this
automatically, since completed lists are skipped and failed ones are not.

---

## 6. Incremental sync

```bash
spconnect sync
```

Uses `GetListItemChangesSinceToken`. The first pass per list stores a token;
later runs receive only inserts, updates and **deletes** — deleted items are
removed from both JSONL files.

Falls back to a full crawl, with a warning recorded in the manifest, when:
- no token is stored yet for that list,
- the token was rejected or expired,
- the server build predates WSS 3.0.

`sync` is always SOAP, even with `SP_API_MODE=odata` — OData has no change feed.

---

## 7. Logging and diagnosis

| Flag | Effect |
|---|---|
| *(default)* | Step narration on stdout, `INFO` on stderr |
| `-v` | `DEBUG`: every HTTP request/response with status, duration, bytes; every SOAP operation and OData query |
| `-vv` | Also captures request/response **bodies** to `landing/_trace.log`, mode `0600` |
| `-q` | Narration off |
| `--log-format json` | One JSON object per line; narration auto-off |

`-vv` is the setting for "the server is answering with something I do not
recognise". Bodies never travel on stderr — see [security.md](security.md).

```bash
spconnect -vv probe 2> probe.log
```

---

## 8. Politeness

The farm is old. Two settings govern load:

- **`SP_REQUESTS_PER_SECOND`** (default `3`) rate-limits *every* outbound
  request, downloads included. This is the important one.
- **`SP_CONCURRENCY`** (default `2`) bounds parallel file downloads within an
  item. Lists are crawled sequentially so progress and checkpointing stay
  deterministic.

A full crawl is meant to be slow. `--dry-run` tells you how slow before you
commit.

---

## 9. Operational checklist

Before the first production crawl:

- [ ] `pytest` passes on the target machine
- [ ] `spconnect probe` exits `0`, and the web count matches expectations
- [ ] `spconnect crawl --dry-run` estimate is acceptable
- [ ] `spconnect verify-time` confirmed against the SharePoint UI
- [ ] Item-level permissions reviewed — see `lists_with_unique_scopes`
- [ ] Landing zone has enough disk for items **plus files**
- [ ] `SP_AUTH_MODE=integrated`, or `.env` is `0600` and not committed

After every crawl:

- [ ] `_manifest.json` → `counts.lists_failed` is `0`
- [ ] `_manifest.json` → `errors[]` reviewed
- [ ] `spconnect stats` item count matches expectations

---

## See also

- [troubleshooting.md](troubleshooting.md) — symptom → cause → fix
- [configuration.md](configuration.md) — every setting
- [security.md](security.md) — credentials and logs
- [landing-zone.md](landing-zone.md) — the output contract
