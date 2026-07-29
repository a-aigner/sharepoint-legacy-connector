"""Attachment and document-library downloads.

Plain authenticated streaming ``GET``. ``Copy.asmx``'s ``GetItem`` would also
work and returns metadata in the same call, but it base64-encodes the whole
file into memory first; on a library with 200 MB CAD drawings that is a bad
trade. Deliberately not implemented.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .config import Settings, get_logger
from .models import AttachmentRecord
from .transport import Transport, TransportError

log = get_logger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_NAME = 120


def filename_from_url(url: str) -> str:
    """Last path segment, percent-decoded and made filesystem-safe."""
    path = urlsplit(url).path
    raw = unquote(path.rsplit("/", 1)[-1]) if path else ""
    return sanitise_filename(raw or "unnamed")


def sanitise_filename(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    if not cleaned:
        cleaned = "unnamed"
    if len(cleaned) > _MAX_NAME:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 10:
            cleaned = stem[: _MAX_NAME - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:_MAX_NAME]
    return cleaned


def extension_of(name: str) -> str:
    _, dot, ext = name.rpartition(".")
    return ("." + ext.lower()) if dot else ""


def _unique_path(directory: Path, filename: str) -> Path:
    """Avoid clobbering when two attachments on one item share a name."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem, dot, ext = filename.rpartition(".")
    base, suffix = (stem, "." + ext) if dot else (filename, "")
    for index in range(1, 1000):
        candidate = directory / f"{base}({index}){suffix}"
        if not candidate.exists():
            return candidate
    return directory / f"{base}({os.getpid()}){suffix}"


class FileDownloader:
    """Applies the skip policy, streams to disk, hashes on the way through."""

    def __init__(self, transport: Transport, settings: Settings) -> None:
        self.transport = transport
        self.settings = settings

    def skip_reason(self, filename: str, size: int | None = None) -> str | None:
        if not self.settings.download_files:
            return "downloads_disabled"
        ext = extension_of(filename)
        if ext and ext in self.settings.skip_extensions_list:
            return f"extension_excluded:{ext}"
        if size is not None and size > self.settings.max_file_bytes:
            return f"too_large:{size}>{self.settings.max_file_bytes}"
        return None

    def download(self, url: str, dest_dir: Path, filename: str | None = None) -> AttachmentRecord:
        """Fetch one file. Never raises: failures come back as ``skip_reason``."""
        name = sanitise_filename(filename) if filename else filename_from_url(url)
        record = AttachmentRecord(filename=name, url=url)

        reason = self.skip_reason(name)
        if reason:
            record.skip_reason = reason
            log.debug("file.skipped", url=url, reason=reason)
            return record

        dest_dir.mkdir(parents=True, exist_ok=True)
        target = _unique_path(dest_dir, name)
        digest = hashlib.sha256()
        written = 0

        try:
            response = self.transport.get(url, stream=True)
        except TransportError as exc:
            record.skip_reason = f"download_failed:{type(exc).__name__}"
            log.warning("file.download_failed", url=url, error=str(exc))
            return record

        try:
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    if int(declared) > self.settings.max_file_bytes:
                        record.skip_reason = f"too_large:{declared}>{self.settings.max_file_bytes}"
                        log.debug("file.skipped", url=url, reason=record.skip_reason)
                        return record
                except ValueError:
                    # A malformed Content-Length is not itself a reason to skip;
                    # the streaming guard below still enforces the size limit.
                    pass

            with target.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > self.settings.max_file_bytes:
                        handle.close()
                        target.unlink(missing_ok=True)
                        record.skip_reason = f"too_large:>{self.settings.max_file_bytes}"
                        log.debug("file.skipped", url=url, reason=record.skip_reason)
                        return record
                    digest.update(chunk)
                    handle.write(chunk)
        except Exception as exc:
            target.unlink(missing_ok=True)
            record.skip_reason = f"download_failed:{type(exc).__name__}"
            log.warning("file.download_failed", url=url, error=str(exc))
            return record
        finally:
            response.close()

        record.filename = target.name
        record.bytes = written
        record.sha256 = digest.hexdigest()
        record.downloaded = True
        record.local_path = str(target)
        log.debug("file.downloaded", url=url, bytes=written)
        return record
