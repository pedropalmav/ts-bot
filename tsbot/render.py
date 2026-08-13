"""Turning a finished job into a Telegram message body.

Messages use ``parse_mode=HTML``, deliberately. MarkdownV2 requires escaping
``_ * [ ] ( ) ~ ` > # + - = | { } . !`` -- every one of which shows up in an
ordinary stack trace -- and a single missed character makes the API reject the
whole message with HTTP 400. HTML needs only ``&``, ``<`` and ``>``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

# Telegram's sendMessage cap, in UTF-8 characters after entity parsing.
TELEGRAM_MAX_CHARS = 4096

TRUNCATION_MARKER = "…(truncated)\n"


@dataclass(frozen=True)
class Fields:
    """Raw (unescaped) values for template interpolation."""

    jobid: str
    command: str
    exit_code: int
    status: str
    host: str
    output_file: str
    label: str | None = None
    duration: str | None = None
    detected: str | None = None


class _SafeDict(dict):
    """Leave unknown placeholders visible instead of raising.

    A typo in a user-edited template should produce a slightly odd message,
    not silently suppress the notification entirely.
    """

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_exit_code(code: int) -> str:
    """task-spooler reports -1 both for signal deaths and internal errors."""
    if code == -1:
        return "killed or crashed (signal)"
    return f"exit {code}"


def format_duration(seconds: float | None) -> str | None:
    if seconds is None or seconds < 0:
        return None
    if seconds < 60:
        return f"{seconds:.1f}s"

    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def render(
    template: str,
    fields: Fields,
    log: str | None = None,
    max_chars: int = TELEGRAM_MAX_CHARS,
) -> tuple[str, bool]:
    """Render ``template`` within Telegram's length budget.

    Returns ``(message, log_was_truncated)``. The template's own HTML is left
    alone; only interpolated values are escaped.
    """
    values = _escaped_values(fields)

    # Measure the fixed cost first, then spend whatever is left on the log.
    base = template.format_map(_SafeDict(values, log=""))
    budget = max_chars - len(base)

    if log is None or not log.strip():
        return base[:max_chars], False

    if budget <= 0:
        # No room for any log at all; the header alone overflows.
        return base[:max_chars], True

    fitted, truncated = _fit_log(log, budget)
    message = template.format_map(_SafeDict(values, log=fitted))
    return message[:max_chars], truncated


def _escaped_values(fields: Fields) -> dict[str, str]:
    def esc(value: str) -> str:
        # quote=False: leaving " as-is keeps stack traces readable, and
        # Telegram only requires & < > to be escaped.
        return html.escape(value, quote=False)

    label = f"[{esc(fields.label)}] " if fields.label else ""
    duration = f" · {esc(fields.duration)}" if fields.duration else ""
    detected = f"<i>detected: {esc(fields.detected)}</i>\n" if fields.detected else ""

    return {
        "jobid": esc(fields.jobid),
        "command": esc(fields.command),
        "exit_code": esc(format_exit_code(fields.exit_code)),
        "status": esc(fields.status),
        "host": esc(fields.host),
        "output_file": esc(fields.output_file),
        "label": label,
        "duration": duration,
        "detected": detected,
    }


def _fit_log(log: str, budget: int) -> tuple[str, bool]:
    """Escape ``log``, trimming from the front until it fits ``budget``.

    Trimming happens on the raw text, never the escaped text -- slicing escaped
    output can cut an entity like ``&amp;`` in half and corrupt the markup.
    """
    escaped = html.escape(log, quote=False)
    if len(escaped) <= budget:
        return escaped, False

    marker = html.escape(TRUNCATION_MARKER, quote=False)
    lines = log.split("\n")

    # Drop whole lines from the top; the tail is where the error lives.
    while lines:
        lines.pop(0)
        if not lines:
            break
        candidate = marker + html.escape("\n".join(lines), quote=False)
        if len(candidate) <= budget:
            return candidate, True

    # A single line is still too long on its own: cut it mid-line.
    return marker + _hard_cut(log, budget - len(marker)), True


def _hard_cut(text: str, budget: int) -> str:
    if budget <= 0:
        return ""

    raw = text[-budget:]
    # Escaping expands the text, so shrink until the escaped form fits.
    while raw:
        escaped = html.escape(raw, quote=False)
        overshoot = len(escaped) - budget
        if overshoot <= 0:
            return escaped
        raw = raw[max(1, overshoot) :]
    return ""
