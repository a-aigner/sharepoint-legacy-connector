"""Request/response body capture, written to a private file.

Bodies never go through the log stream. stderr is the thing most likely to be
redirected into a shared file, piped into a collector, or pasted into a ticket —
and a body can carry session cookies, a proxy's auth echo, or list data that is
itself sensitive. So ``-vv`` writes bodies *here*, at ``0600``, and logs only
the path and a sequence number.

The scrubber still runs over everything written. This file is a second control,
not a replacement for the first.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from types import TracebackType
from typing import IO

from .config import get_logger, scrub

log = get_logger(__name__)

#: Owner read/write only.
PRIVATE_MODE = 0o600


def open_private(path: Path, mode: str = "a") -> IO[str]:
    """Open a file that only the owning user can read.

    The mode is applied at ``open`` time rather than afterwards, so there is no
    window in which the file exists world-readable. On Windows the POSIX mode is
    largely advisory — inherited directory ACLs are what actually apply there.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    fd = os.open(path, flags, PRIVATE_MODE)
    with contextlib.suppress(OSError):  # some filesystems refuse chmod
        os.chmod(path, PRIVATE_MODE)
    return os.fdopen(fd, mode, encoding="utf-8")


def write_private_bytes(path: Path, payload: bytes) -> Path:
    """Write bytes to a ``0600`` file. Used for captured error bodies."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)
    with contextlib.suppress(OSError):
        os.chmod(path, PRIVATE_MODE)
    return path


class BodyTrace:
    """Append-only sink for request/response bodies."""

    def __init__(self, path: Path, max_chars: int = 2000) -> None:
        self.path = Path(path)
        self.max_chars = max_chars
        self.entries = 0
        self._handle: IO[str] | None = None

    def _ensure_open(self) -> IO[str]:
        if self._handle is None:
            self._handle = open_private(self.path)
            self._handle.write(
                f"# spconnect body trace — pid {os.getpid()} — argv: {' '.join(sys.argv[1:])}\n"
                f"# This file may contain list data and response headers. Mode 0600.\n"
            )
            log.warning(
                "trace.enabled",
                path=str(self.path),
                detail="request/response bodies are being written to a private file",
            )
        return self._handle

    def write(self, seq: int, kind: str, meta: str, body: str | bytes) -> None:
        """Record one body. Truncated, scrubbed, never echoed to the log."""
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        if len(text) > self.max_chars:
            text = f"{text[: self.max_chars]}\n… [{len(text) - self.max_chars} more chars truncated]"

        handle = self._ensure_open()
        handle.write(f"\n===== #{seq} {kind} {meta} =====\n")
        handle.write(scrub(text))
        handle.write("\n")
        handle.flush()
        self.entries += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self) -> BodyTrace:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
