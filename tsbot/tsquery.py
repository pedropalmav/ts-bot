"""Asking task-spooler how long a job ran.

The hook is handed only ``jobid errorlevel output_filename command`` -- no
timing. It cannot be derived from the output file either: ``st_birthtime`` is
unavailable on Linux, and ``st_ctime`` moves with every write. So we ask the
server.

This is safe despite the hook running *before* ``c_end_of_job()``: ``ts -i`` is
a query answered by the server process, which is not blocked by our client. It
is still guarded by a short timeout, because anything slow here holds the job's
slot open.
"""

from __future__ import annotations

import re
import subprocess
import time

QUERY_TIMEOUT_SECONDS = 2.0

# "Time run: 12.345000s" -- present once the job is fully accounted for.
_TIME_RUN_RE = re.compile(r"^\s*Time run:\s*([0-9.]+)s", re.MULTILINE)

# "Start time: Thu Aug  7 12:00:01 2026" -- ctime(3) format.
_START_TIME_RE = re.compile(r"^\s*Start time:\s*(.+?)\s*$", re.MULTILINE)

_CTIME_FORMAT = "%a %b %d %H:%M:%S %Y"


def job_duration(ts_command: str, jobid: str) -> float | None:
    """Seconds the job ran, or None if it cannot be determined.

    Every failure mode -- missing binary, timeout, unrecognised output --
    returns None so the caller simply omits the duration.
    """
    output = _run_info(ts_command, jobid)
    if output is None:
        return None

    match = _TIME_RUN_RE.search(output)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            pass

    return _duration_from_start_time(output)


def _run_info(ts_command: str, jobid: str) -> str | None:
    try:
        completed = subprocess.run(
            [ts_command, "-i", str(jobid)],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None

    if completed.returncode != 0:
        return None
    return completed.stdout


def _duration_from_start_time(output: str) -> float | None:
    match = _START_TIME_RE.search(output)
    if not match:
        return None

    try:
        # strptime treats literal spaces in the format as \s+, so the
        # single-digit-day double space in ctime output parses fine.
        started = time.mktime(time.strptime(match.group(1), _CTIME_FORMAT))
    except (ValueError, OverflowError):
        return None

    elapsed = time.time() - started
    return elapsed if elapsed >= 0 else None
