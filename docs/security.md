# Security

What this connector protects, how, and — as importantly — what it does not.

---

## 1. It is read-only

There is no code path that writes to SharePoint. No `UpdateListItems`, no
`Copy.asmx`, no PUT, no POST that mutates. The seven SOAP operations and the
OData backend are all reads.

---

## 2. Credentials

### Use `SP_AUTH_MODE=integrated`

The strongest option, and the recommended one. The process authenticates as the
identity it is already running under — Windows SSPI, or an existing Kerberos
ticket.

**No password is read, stored, or held in memory.** There is nothing in `.env`,
nothing in the process, nothing that can reach a log, a backup, or a core dump.

```bash
pip install 'spconnect[windows]'      # domain-joined Windows
pip install 'spconnect[kerberos]'     # elsewhere, after kinit
```

Then run the crawl **as** the service account and leave `SP_USERNAME` /
`SP_PASSWORD` blank.

This is a **control**. Everything in §3 is a *mitigation* — it reduces the
chance of a leak, it does not eliminate the possibility. If a secret never
exists, there is nothing to reason about.

### The modes that do hold a secret

| Mode | Password in memory? | Transmitted? |
|---|---|---|
| `integrated` | **No** | Never — token-based |
| `ntlm` | Yes | **No** — challenge/response, the password never crosses the wire |
| `basic` | Yes | **Yes, on every request.** Logs a warning. Use only over https. |
| `anonymous` | No | — |

Prefer `integrated`, then `ntlm`. `basic` is supported because some farms of this
vintage only offer it, not because it is a reasonable choice.

### If you must use a password

- `.env` is in `.gitignore`. Keep it `0600`.
- `SP_PASSWORD` is held as a pydantic `SecretStr`, so an accidental `repr()`
  or `print()` of the settings object yields `***REDACTED***`.
- It is redacted in `_manifest.json`, which is written on every run.
- It is registered with the log scrubber before the first request.

---

## 3. Secrets in logs — the backstop

Relevant only when a password exists. It covers:

**The password and its encoded forms.** Registered at transport construction:
the plaintext, the URL-encoded form, `base64(password)`, and
`base64("user:password")` — the Basic auth blob, in which the plaintext **never
appears at all**.

**Longest-first replacement.** Base64 aligns on 3-byte boundaries, so
`b64("pass")` can be a literal substring of `b64("user:pass")`. Replacing the
short secret first fragments the long one and leaves partial credential material
behind. Ordering is part of the control, not an implementation detail.

**Recursively.** A secret nested inside a dict or list is still a leaked secret.

**Rendered tracebacks.** The scrubber runs *after* `format_exc_info`, because
that processor turns an exception into a string — a credential inside an
exception message would otherwise sail straight past.

**Headers by allowlist.** Anything not explicitly known-safe is redacted. A
denylist cannot cover a header nobody thought of: a reverse proxy's
`X-Forwarded-Authorization`, a vendor token. `WWW-Authenticate` is reduced to its
scheme names, since the value can carry a Negotiate/GSSAPI token.

Each of those is a regression test in `tests/test_logging.py`, including one
where the mock server **echoes the password back in its response body**.

### What it cannot cover

Scrubbing is a blocklist over an infinite space of encodings. It catches the
forms listed above. It cannot catch an encoding nobody anticipated — a
hex-encoded password, a hash, a custom obfuscation by some middlebox.

That is the whole argument for `integrated`.

---

## 4. Captured bodies

`-vv` writes request and response bodies to `SP_TRACE_FILE` (default
`landing/_trace.log`), **never to the log stream**. stderr is the stream most
likely to be redirected into a shared file, piped to a collector, or pasted into
a ticket.

- Opened with mode `0600` **at `open()` time**, so there is no window in which
  the file exists world-readable.
- Bodies are truncated to `SP_LOG_BODY_CHARS`.
- The scrubber still runs over everything written — a second control, not a
  replacement for the first.
- `_last_bad_response.xml` is written the same way, since captured error bodies
  routinely contain session cookies.

> On Windows the POSIX mode is largely advisory. Inherited directory ACLs are
> what actually apply — place the landing zone somewhere appropriately
> restricted.

**These files can contain business data.** Review before sharing.

---

## 5. Item-level permissions are flattened

The most consequential security property, and it is not about credentials.

The connector crawls as **one identity**. If different technicians currently see
different subsets of cases, that distinction does not survive extraction: every
item lands in the same place with no ACL.

Any list reporting `HasUniqueScopes="True"` is:

- called out in the `probe` and crawl summaries,
- recorded in `_manifest.json` under `lists_with_unique_scopes`.

If you index that content and expose it to a broad audience, **you have widened
access to it.** That is a product decision, not a technical detail — escalate it
rather than absorbing it.

---

## 6. Transport security

`SP_ALLOW_LEGACY_TLS=true` mounts an adapter permitting **TLS 1.0** and
`DEFAULT@SECLEVEL=0` ciphers, and `SP_VERIFY_SSL=false` disables certificate
verification.

Both are genuine weakenings, enabled by default because a 2010-era IIS farm
frequently offers nothing better. A prominent warning is logged whenever legacy
TLS is active.

- On **plain HTTP**, neither applies — there is no TLS to weaken, and the
  traffic is already unencrypted on the wire.
- On https, prefer `SP_VERIFY_SSL=true` if the farm's certificate chains to
  something you trust.

Both settings only affect traffic to this farm, on a session that talks to
nothing else.

---

## 7. What the landing zone contains

Extracted business data — potentially every service case, customer name and
attachment in the CRM. Treat the directory with the same care as the source
system.

It contains **no credentials**: `_manifest.json` redacts the password, and there
is a test asserting `SP_PASSWORD` appears nowhere in it.

`landing/` is gitignored.

---

## 8. Reporting a problem

If you find a leak path the scrubber misses, that is a real bug. Reproduce it
with a fake password, capture the log line, and report it — do not send a log
containing a real credential to demonstrate that logs can contain credentials.

---

## See also

- [operations.md](operations.md) — running it safely
- [decisions.md](decisions.md#d9--integrated-auth-as-the-recommended-mode) — why
  integrated auth was added, and what testing the scrubber revealed
