"""Reading and mining task-spooler's job output file.

Job logs are routinely hundreds of megabytes (training runs, verbose builds),
so everything here is bounded: we seek to the tail rather than reading a file
whole, and the gzip path streams through a fixed-size buffer.
"""

from __future__ import annotations

import gzip
import re
from pathlib import Path

GZIP_MAGIC = b"\x1f\x8b"

# Where a Python traceback begins. Anything after the *last* one is the error
# that actually killed the job.
_TRACEBACK_RE = re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE)

# The exception line that closes a traceback, e.g. "RuntimeError: CUDA OOM".
_EXCEPTION_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Interrupt))\b", re.MULTILINE)

# Generic error markers for non-Python jobs (compilers, shell scripts, CUDA).
_ERROR_LINE_RE = re.compile(
    r"^.*?(?P<token>\b(?:ERROR|FATAL|CRITICAL|Segmentation fault|Killed"
    r"|error:|undefined reference|command not found)\b)",
    re.MULTILINE | re.IGNORECASE,
)

# How many lines of lead-in to keep before a generic error marker, so the
# excerpt has some context rather than starting mid-sentence.
_ERROR_CONTEXT_LINES = 8


def error_source(output_file: str | Path) -> Path | None:
    """Pick which file holds the job's error output.

    When a job is queued with ``ts -E`` stderr is written to ``<ofname>.e``
    (execute.c:185) -- a path the hook is never told about. If that file exists
    and has content it *is* the error stream, so prefer it.
    """
    main = Path(output_file)
    separate_stderr = Path(f"{main}.e")

    try:
        if separate_stderr.is_file() and separate_stderr.stat().st_size > 0:
            return separate_stderr
    except OSError:
        pass

    try:
        if main.is_file():
            return main
    except OSError:
        pass

    # Queued with `ts -n` (output discarded), or the file is already gone.
    return None


def read_tail(path: str | Path, max_bytes: int) -> str | None:
    """Return at most the last ``max_bytes`` of ``path`` as text.

    Returns None if the file cannot be read at all. Handles gzip-compressed
    output, which task-spooler produces for jobs queued with ``-g``.
    """
    path = Path(path)
    try:
        with path.open("rb") as handle:
            magic = handle.read(len(GZIP_MAGIC))
            if magic == GZIP_MAGIC:
                data, truncated = _tail_gzip(path, max_bytes)
            else:
                data, truncated = _tail_plain(handle, max_bytes)
    except OSError:
        return None

    text = data.decode("utf-8", errors="replace")

    # A byte-offset seek almost certainly lands mid-line; drop that fragment.
    if truncated:
        newline = text.find("\n")
        text = text[newline + 1 :] if newline != -1 else text

    return text


def _tail_plain(handle, max_bytes: int) -> tuple[bytes, bool]:
    handle.seek(0, 2)
    size = handle.tell()
    read_from = max(0, size - max_bytes)
    handle.seek(read_from)
    return handle.read(), read_from > 0


def _tail_gzip(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    """Stream-decompress, keeping only the trailing window in memory."""
    buffer = b""
    truncated = False
    with gzip.open(path, "rb") as gz:
        while chunk := gz.read(65536):
            buffer += chunk
            if len(buffer) > max_bytes:
                buffer = buffer[-max_bytes:]
                truncated = True
    return buffer, truncated


def last_lines(text: str, count: int) -> str:
    """The final ``count`` non-empty-trailing lines of ``text``."""
    lines = text.rstrip("\n").split("\n")
    return "\n".join(lines[-count:]) if count > 0 else ""


def extract_error(text: str, max_lines: int) -> tuple[str, str] | None:
    """Find the part of ``text`` that explains why the job failed.

    Returns ``(excerpt, detected_name)`` or None when nothing matches, in which
    case the caller falls back to a plain tail.
    """
    if not text.strip():
        return None

    traceback = _extract_traceback(text, max_lines)
    if traceback is not None:
        return traceback

    return _extract_error_line(text, max_lines)


def _extract_traceback(text: str, max_lines: int) -> tuple[str, str] | None:
    starts = list(_TRACEBACK_RE.finditer(text))
    if not starts:
        return None

    excerpt = text[starts[-1].start() :].rstrip("\n")

    # Name the excerpt after the exception that closes it, when there is one.
    exceptions = list(_EXCEPTION_RE.finditer(excerpt))
    detected = exceptions[-1].group(1) if exceptions else "Traceback"

    return _clamp_lines(excerpt, max_lines), detected


def _extract_error_line(text: str, max_lines: int) -> tuple[str, str] | None:
    matches = list(_ERROR_LINE_RE.finditer(text))
    if not matches:
        return None

    last = matches[-1]
    lines = text.rstrip("\n").split("\n")

    # Translate the match offset into a line index.
    line_index = text.count("\n", 0, last.start())
    start = max(0, line_index - _ERROR_CONTEXT_LINES)

    excerpt = "\n".join(lines[start:])
    return _clamp_lines(excerpt, max_lines), last.group("token").strip()


def _clamp_lines(text: str, max_lines: int) -> str:
    lines = text.split("\n")
    if len(lines) <= max_lines:
        return text
    # Keep the end: the exception line matters more than the frames above it.
    return "\n".join(lines[-max_lines:])
