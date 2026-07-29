"""Step-by-step console narration.

Separate from :mod:`structlog` on purpose. Structured logs are for grepping
afterwards; this is for an operator watching a terminal on a locked-down box,
wanting to know *which part just succeeded* before the next one hangs.

    [2/8] Authenticate (ntlm as CONTOSO\\svc) ........... OK      0.31s
    [3/8] Server build number .......................... OK      0.04s  14.0.4762.1000
    [4/8] Enumerate webs ............................... FAILED  1.20s
          SoapResponseError: no <GetAllSubWebCollectionResult> element …

Dot leaders and a fixed result column mean a failed step is findable by eye in
a wall of output, which is the whole point.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import typer

LABEL_WIDTH = 52
INDENT = "      "


@dataclass
class StepResult:
    """Handle a step body uses to attach detail to its own result line."""

    label: str
    details: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    ok: bool = True
    error: str | None = None

    def detail(self, text: str) -> None:
        """Short text appended to the result line."""
        if text:
            self.details.append(str(text))

    def note(self, text: str) -> None:
        """A line printed underneath the step, indented."""
        if text:
            self.notes.append(str(text))


class StepReporter:
    """Numbered progress narration. Silent when disabled."""

    def __init__(self, enabled: bool = True, total: int | None = None) -> None:
        self.enabled = enabled
        self.total = total
        self.index = 0
        self.failed: list[str] = []
        self.started = time.monotonic()

    # ---- output primitives ----

    def _echo(self, message: str = "") -> None:
        if self.enabled:
            typer.echo(message)

    def heading(self, text: str) -> None:
        self._echo("")
        self._echo(text)
        self._echo("─" * min(len(text), 72))

    def info(self, text: str) -> None:
        self._echo(f"{INDENT}{text}")

    # ---- steps ----

    @contextmanager
    def step(self, label: str) -> Iterator[StepResult]:
        """Run a step, printing one aligned result line whatever happens.

        Exceptions are re-raised after being reported: this narrates, it does
        not swallow.
        """
        self.index += 1
        counter = f"[{self.index}/{self.total}]" if self.total else f"[{self.index}]"
        result = StepResult(label=label)
        started = time.monotonic()
        try:
            yield result
        except Exception as exc:
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            self._render(counter, label, result, time.monotonic() - started)
            self.failed.append(label)
            raise
        else:
            self._render(counter, label, result, time.monotonic() - started)

    def _render(self, counter: str, label: str, result: StepResult, elapsed: float) -> None:
        head = f"{counter} {label} "
        leader = "." * max(3, LABEL_WIDTH - len(head))
        status = "OK    " if result.ok else "FAILED"
        detail = ("  " + "  ".join(result.details)) if result.details else ""
        self._echo(f"{head}{leader} {status} {elapsed:6.2f}s{detail}")

        if result.error:
            for line in result.error.splitlines():
                self._echo(f"{INDENT}{line}")
        for note in result.notes:
            for line in str(note).splitlines():
                self._echo(f"{INDENT}{line}")

    # ---- summary ----

    def done(self, message: str = "") -> None:
        elapsed = time.monotonic() - self.started
        self._echo("")
        if self.failed:
            self._echo(f"{len(self.failed)} step(s) FAILED in {elapsed:.2f}s: {', '.join(self.failed)}")
        else:
            self._echo(message or f"All steps OK in {elapsed:.2f}s")


def format_bytes(count: int | float) -> str:
    value = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def truncate(text: Any, limit: int = 2000) -> str:
    """Body text for a log line: bounded, single-purpose, never a surprise."""
    rendered = text if isinstance(text, str) else str(text)
    if len(rendered) <= limit:
        return rendered
    return f"{rendered[:limit]}… [{len(rendered) - limit} more chars]"
