"""Resumable crawl state.

Written after every page, atomically. A crawl interrupted at any point must
resume from the last completed page — on a farm where a full pass can take
hours, "start over" is not an acceptable failure mode.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

ListStatus = Literal["pending", "in_progress", "complete", "failed"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class ListState(BaseModel):
    web_url: str = ""
    list_title: str = ""
    last_full_crawl: datetime | None = None
    last_sync: datetime | None = None
    last_item_id: int = 0
    items_written: int = 0
    change_token: str | None = None
    status: ListStatus = "pending"
    error: str | None = None


class CrawlState(BaseModel):
    """``_state.json``."""

    version: int = 1
    updated_at: datetime | None = None
    lists: dict[str, ListState] = Field(default_factory=dict)


class StateStore:
    """Thread-safe, atomically-persisted :class:`CrawlState`."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.state = self._load()

    def _load(self) -> CrawlState:
        if not self.path.exists():
            return CrawlState()
        try:
            return CrawlState.model_validate_json(self.path.read_text(encoding="utf-8"))
        except Exception:
            backup = self.path.with_suffix(self.path.suffix + ".corrupt")
            self.path.replace(backup)
            return CrawlState()

    def get(self, list_guid: str) -> ListState:
        with self._lock:
            return self.state.lists.setdefault(list_guid, ListState())

    def update(self, list_guid: str, **changes: object) -> ListState:
        with self._lock:
            entry = self.state.lists.setdefault(list_guid, ListState())
            for key, value in changes.items():
                setattr(entry, key, value)
            return entry

    def save(self) -> None:
        """Temp file + ``os.replace`` — never a half-written state file."""
        with self._lock:
            self.state.updated_at = utcnow()
            payload = self.state.model_dump_json(indent=2)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise

    def reset(self, list_guid: str) -> ListState:
        with self._lock:
            entry = ListState()
            self.state.lists[list_guid] = entry
            return entry
