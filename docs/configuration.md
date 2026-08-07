# Configuration reference

Every setting, its type, its default, and when to change it.

**This table is generated from `Settings` in `src/spconnect/config.py`** by
`tests/test_docs.py`, which fails if the two drift apart. If you add a setting
and this file is not updated, the test suite will say so.

---

## Precedence

```
CLI flag  >  environment variable  >  .env file  >  built-in default
```

A CLI flag that was not supplied is ignored rather than overriding an env var.

Load a different file with `--env-file /path/to/other.env`.

---

## Settings

### Connection

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_BASE_URL` | str | `http://localhost` | Root URL of the web application or site collection. Trailing slash stripped automatically. |
| `SP_AUTH_MODE` | choice | `ntlm` | `integrated` \| `ntlm` \| `basic` \| `anonymous`. **`integrated` is recommended** — no password anywhere. `.env.example` ships it; the built-in default stays `ntlm` because it needs no extra packages. |
| `SP_USERNAME` | str | `(empty)` | `DOMAIN\username` for NTLM. **One backslash.** Unused by `integrated`/`anonymous`. |
| `SP_PASSWORD` | SecretStr | `(empty)` | Unused by `integrated`/`anonymous`. Held as `SecretStr`; never logged, never in the manifest. |

### Transport (legacy servers)

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_ALLOW_LEGACY_TLS` | bool | `True` | Permit TLS 1.0 and `DEFAULT@SECLEVEL=0` ciphers. **https only** — irrelevant over plain HTTP. Logs a warning when active. |
| `SP_VERIFY_SSL` | bool | `False` | Certificate verification. Off by default because these farms usually have self-signed certs. |
| `SP_NTLM_PRIME_CONNECTION` | bool | `True` | Retry a 401'd SOAP POST once on a connection authenticated by a bodyless GET. Only fires when the GET succeeds where the POST failed, so it never spends a second attempt on a genuinely bad credential. See [troubleshooting](troubleshooting.md). |
| `SP_TIMEOUT_SECONDS` | float | `120.0` | Per-request timeout. |
| `SP_MAX_RETRIES` | int | `5` | Attempts for retryable failures (5xx, timeouts, connection resets). Never applied to 401/403/404 or SOAP faults. |
| `SP_BACKOFF_BASE_SECONDS` | float | `2.0` | Exponential backoff multiplier, capped at 120s. |

### Politeness

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_CONCURRENCY` | int | `2` | Parallel **file downloads within one item**. Lists are crawled sequentially by design. |
| `SP_REQUESTS_PER_SECOND` | float | `3.0` | Global rate limit on *every* outbound request, downloads included. The main politeness dial. |

### Crawl scope

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_INCLUDE_WEBS` | str | `(empty)` | Comma-separated, case-insensitive substring match on URL or title. Empty = all. |
| `SP_EXCLUDE_WEBS` | str | `(empty)` | Same matching. Applied after includes. |
| `SP_INCLUDE_LISTS` | str | `(empty)` | Matches list title **or** GUID. |
| `SP_EXCLUDE_LISTS` | str | `(empty)` | Wins over includes. |
| `SP_INCLUDE_HIDDEN_LISTS` | bool | `False` | The system-list block list still applies regardless. |
| `SP_INCLUDE_DOCUMENT_LIBRARIES` | bool | `True` | Set false to skip `BaseType=1` lists entirely. |

### Item source

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_API_MODE` | choice | `soap` | `soap` \| `odata`. Selects the **item fetch only** — discovery, schema and deletes are always SOAP. |
| `SP_ODATA_EXPAND_LOOKUPS` | bool | `True` | `$expand` lookup columns so labels arrive with ids. Retried without it automatically if the server 500s. |
| `SP_PAGE_SIZE` | int | `200` | Rows per page. Also `$top` in OData mode. |

### Files

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_DOWNLOAD_FILES` | bool | `True` | Attachments and document-library files. When false, entries are recorded with `skip_reason=downloads_disabled`. |
| `SP_MAX_FILE_MB` | float | `100.0` | Enforced from `Content-Length` and again mid-stream, so a lying header cannot bypass it. |
| `SP_SKIP_EXTENSIONS` | str | `.exe,.dll,.msi,.iso` | Comma-separated. Leading dot optional; matching is case-insensitive. |

### Output

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_LANDING_DIR` | Path | `landing` | Root of the output contract. |
| `SP_STATE_FILE` | Path | `landing/_state.json` | Resume checkpoints. Moves with `--landing-dir` unless set explicitly. |

### Logging

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_LOG_LEVEL` | str | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR`. `-v` sets DEBUG. |
| `SP_LOG_FORMAT` | choice | `console` | `console` \| `json`. `json` disables the step narration. |
| `SP_LOG_BODIES` | bool | `False` | Capture request/response bodies. `-vv` sets this. Written to `trace_file`, **never** to the log stream. |
| `SP_TRACE_FILE` | path? | `None` | Defaults to `{landing_dir}/_trace.log`. Created mode `0600`. |
| `SP_LOG_BODY_CHARS` | int | `2000` | Truncation limit per captured body. |
| `SP_SHOW_STEPS` | bool | `True` | The numbered step narration. `-q` disables it. |

### Tests

| Setting | Type | Default | Notes |
|---|---|---|---|
| `SP_LIVE_TESTS` | bool | `False` | Set `1` to enable `tests/test_live_smoke.py`, which hits the real server. |

---

## Derived values

Not settings, but computed from them — useful when reading the code:

| Property | Meaning |
|---|---|
| `include_webs_list` and friends | The comma-separated settings, split and trimmed |
| `skip_extensions_list` | Normalised to lowercase with a leading dot |
| `max_file_bytes` | `max_file_mb` × 1024 × 1024 |
| `needs_password` | True only for `ntlm` and `basic` — the modes that hold a secret |
| `resolved_trace_file` | `trace_file` or `{landing_dir}/_trace.log` |

---

## Common configurations

**Recommended production setup** — no secret anywhere:

```dotenv
SP_BASE_URL=http://sharepoint.intern.example.de
SP_AUTH_MODE=integrated
SP_LANDING_DIR=/data/sharepoint-landing
SP_REQUESTS_PER_SECOND=3
```

**First contact, maximum diagnosis:**

```bash
spconnect -vv --include-lists "Servicefälle" --page-size 10 crawl --dry-run
```

**Metadata only, no file downloads** — much faster, useful for a first pass:

```bash
spconnect --no-download-files crawl
```

**Comparing backends:**

```bash
spconnect crawl --api-mode soap  --landing-dir ./landing-soap --include-lists "X"
spconnect crawl --api-mode odata --landing-dir ./landing-rest --include-lists "X"
```

---

## See also

- [operations.md](operations.md) — how to run it
- [security.md](security.md) — credential handling
