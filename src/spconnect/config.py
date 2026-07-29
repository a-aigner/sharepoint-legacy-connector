"""Configuration and logging setup.

Precedence, highest first: CLI flag > real environment variable > ``.env`` > default.
The CLI layer implements the first level by calling :func:`load_settings` with
``overrides``; ``pydantic-settings`` implements the rest.

``SP_PASSWORD`` must never reach a log line, the manifest, or ``repr()``.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["ntlm", "basic", "anonymous"]
LogFormat = Literal["console", "json"]

REDACTED = "***REDACTED***"


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
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(levelno),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Typed accessor so call sites do not have to think about structlog generics."""
    return structlog.get_logger(name)
