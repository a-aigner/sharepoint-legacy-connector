"""Configuration and logging setup.

Precedence, highest first: CLI flag > real environment variable > ``.env`` > default.
The CLI layer implements the first level by calling :func:`load_settings` with
``overrides``; ``pydantic-settings`` implements the rest.

``SP_PASSWORD`` must never reach a log line, the manifest, or ``repr()``.
"""

from __future__ import annotations

import base64
import logging
import sys
from collections.abc import Mapping, MutableMapping
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import structlog
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: ``integrated`` uses the *current process identity* (Windows SSPI or a
#: Kerberos ticket) and needs no password at all — the strongest option, because
#: a secret that never enters the process cannot leak from it.
AuthMode = Literal["integrated", "ntlm", "basic", "anonymous"]
ApiMode = Literal["soap", "odata"]
LogFormat = Literal["console", "json"]

REDACTED = "***REDACTED***"

#: Secrets registered at runtime so the log pipeline can scrub them from *any*
#: event, including request/response bodies. Belt and braces: the acceptance
#: criterion is that SP_PASSWORD appears nowhere in logs, and relying on every
#: future call site to remember that is not a control.
_SECRETS: set[str] = set()


def register_secret(value: str | None, *, username: str | None = None) -> None:
    """Register a value — and its common encodings — for scrubbing.

    A plain substring match misses the encoded forms a credential actually
    travels in, so the derived variants are registered too. Notably the Basic
    auth blob: ``base64("user:pass")`` contains no verbatim password at all.
    """
    if not value or len(value) < 3:
        return
    _SECRETS.add(value)
    _SECRETS.add(quote(value, safe=""))
    _SECRETS.add(base64.b64encode(value.encode()).decode())
    for user in filter(None, (username,)):
        for pair in (f"{user}:{value}",):
            _SECRETS.add(base64.b64encode(pair.encode()).decode())
            _SECRETS.add(quote(pair, safe=""))
    _SECRETS.discard("")


def scrub(text: str) -> str:
    """Replace registered secrets, **longest first**.

    Order matters: base64 encoding aligns on 3-byte boundaries, so
    ``b64("user:pass")`` can literally contain ``b64("pass")``. Replacing the
    short one first fragments the long one and leaves partial credential
    material in the log.
    """
    for secret in sorted(_SECRETS, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, REDACTED)
    return text


def scrub_value(value: Any) -> Any:
    """Scrub recursively. A secret nested in a dict is still a leaked secret."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, Mapping):
        return {k: scrub_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set):
        return type(value)(scrub_value(v) for v in value)
    return value


def _scrub_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in event_dict.items():
        event_dict[key] = scrub_value(value)
    return event_dict


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated setting into a clean list. Empty string -> []."""
    return [part.strip() for part in value.split(",") if part.strip()]


class Settings(BaseSettings):
    """All connector configuration. Every field maps to an ``SP_``-prefixed env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="SP_",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Connection ----
    base_url: str = "http://localhost"
    auth_mode: AuthMode = "ntlm"
    username: str = ""
    password: SecretStr = SecretStr("")

    # ---- Transport quirks ----
    allow_legacy_tls: bool = True
    verify_ssl: bool = False
    #: Recover from farms where a SOAP POST is refused but a bodyless GET to the
    #: same endpoint is not. NTLM authenticates a *connection*, and IIS commonly
    #: drops that connection when it 401s a request carrying a body, which fails
    #: the handshake for POSTs while leaving GETs working. Priming the connection
    #: with a GET first gets the handshake done where it can succeed.
    ntlm_prime_connection: bool = True
    timeout_seconds: float = 120.0
    max_retries: int = 5
    backoff_base_seconds: float = 2.0

    # ---- Politeness ----
    concurrency: int = Field(default=2, ge=1)
    requests_per_second: float = Field(default=3.0, gt=0)

    # ---- Crawl scope (comma-separated; empty = all) ----
    include_webs: str = ""
    exclude_webs: str = ""
    include_lists: str = ""
    exclude_lists: str = ""
    include_hidden_lists: bool = False
    include_document_libraries: bool = True

    # ---- Item source ----
    #: Which API fetches list items. Web discovery, schema and change tokens are
    #: always SOAP — OData has no equivalent for them.
    api_mode: ApiMode = "soap"
    #: $expand lookup columns so labels come back alongside ids. Some 2010 farms
    #: 500 on wide expands; the crawler retries without it automatically.
    odata_expand_lookups: bool = True

    # ---- Paging ----
    page_size: int = Field(default=200, ge=1)

    # ---- Files ----
    download_files: bool = True
    max_file_mb: float = 100.0
    skip_extensions: str = ".exe,.dll,.msi,.iso"

    # ---- Output ----
    landing_dir: Path = Path("./landing")
    state_file: Path = Path("./landing/_state.json")

    # ---- Logging ----
    log_level: str = "INFO"
    log_format: LogFormat = "console"
    #: Capture request/response bodies. They are written to :attr:`trace_file`
    #: at mode 0600, never to the log stream — stderr is the thing most likely
    #: to end up in a shared file.
    log_bodies: bool = False
    #: Where captured bodies go. Defaults to ``{landing_dir}/_trace.log``.
    trace_file: Path | None = None
    #: How much of each body to log.
    log_body_chars: int = 2000
    #: Print the numbered step-by-step narration. Off in json log format.
    show_steps: bool = True

    # ---- Tests ----
    live_tests: bool = False

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, v: str) -> str:
        return v.upper()

    # ---- Derived views over the comma-separated settings ----

    @property
    def include_webs_list(self) -> list[str]:
        return _split_csv(self.include_webs)

    @property
    def exclude_webs_list(self) -> list[str]:
        return _split_csv(self.exclude_webs)

    @property
    def include_lists_list(self) -> list[str]:
        return _split_csv(self.include_lists)

    @property
    def exclude_lists_list(self) -> list[str]:
        return _split_csv(self.exclude_lists)

    @property
    def skip_extensions_list(self) -> list[str]:
        return [e.lower() if e.startswith(".") else "." + e.lower() for e in _split_csv(self.skip_extensions)]

    @property
    def resolved_trace_file(self) -> Path:
        return self.trace_file or (self.landing_dir / "_trace.log")

    @property
    def needs_password(self) -> bool:
        """Only these modes carry a secret in-process."""
        return self.auth_mode in ("ntlm", "basic")

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    # ---- Redaction ----

    def redacted_dict(self) -> dict[str, Any]:
        """Config snapshot safe to write into ``_manifest.json``."""
        data = self.model_dump(mode="json")
        data["password"] = REDACTED
        return data

    def __repr__(self) -> str:
        parts = ", ".join(f"{k}={v!r}" for k, v in self.redacted_dict().items())
        return f"Settings({parts})"

    __str__ = __repr__


def load_settings(
    env_file: str | Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Build :class:`Settings`, applying CLI ``overrides`` at the highest precedence.

    ``overrides`` values that are ``None`` are ignored, so a CLI flag that was
    not supplied does not clobber an env var.
    """
    kwargs: dict[str, Any] = {}
    if env_file is not None:
        kwargs["_env_file"] = str(env_file)
    if overrides:
        kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**kwargs)


def setup_logging(level: str = "INFO", fmt: LogFormat = "console") -> None:
    """Configure structlog. Idempotent — safe to call once per CLI invocation."""
    levelno = logging.getLevelName(level.upper())
    if not isinstance(levelno, int):
        levelno = logging.INFO

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # LAST before the renderer, deliberately: format_exc_info turns an
            # exception into a rendered traceback string, and a traceback can
            # carry a credential (a URL with embedded auth, a driver message).
            # Scrubbing earlier would miss it entirely.
            _scrub_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(levelno),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Typed accessor so call sites do not have to think about structlog generics."""
    return structlog.get_logger(name)
