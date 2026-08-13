"""Hook entry point: turn task-spooler's four arguments into a Telegram message.

task-spooler invokes this via ``fork()`` + ``execlp()`` (mail.c:79) and then
``wait()``s for it, from ``execute.c:86`` -- which runs *before*
``c_end_of_job()``. The job's slot therefore stays occupied for as long as this
process lives, which drives two hard rules:

  * never hang -- every network and subprocess call is bounded by a timeout;
  * never crash -- a traceback here lands in the enqueuing shell's stderr.

Both are enforced at the boundary in :func:`main`, which always returns 0.
"""

from __future__ import annotations

import logging
import os
import socket
import sys
from pathlib import Path

from . import logscan, render, telegram, tsquery
from .config import Config, ConfigError, load

LOGGER = logging.getLogger("tsbot")

# Backstop for any socket operation that slips past an explicit timeout.
GLOBAL_SOCKET_TIMEOUT_SECONDS = 30.0

USAGE = "usage: ts-hook <jobid> <errorlevel> <output_file> <command>"


def main(argv: list[str] | None = None) -> int:
    """Always returns 0. See the module docstring for why."""
    _configure_stderr_logging()
    socket.setdefaulttimeout(GLOBAL_SOCKET_TIMEOUT_SECONDS)

    try:
        _run(list(sys.argv[1:] if argv is None else argv))
    except ConfigError as exc:
        LOGGER.error("%s", exc)
    except BaseException:  # noqa: BLE001 - the never-crash boundary
        LOGGER.exception("ts-bot hook failed")

    return 0


def _run(argv: list[str]) -> None:
    if len(argv) < 4:
        LOGGER.error("expected 4 arguments, got %d. %s", len(argv), USAGE)
        return

    jobid, errorlevel, output_file = argv[0], argv[1], argv[2]
    command = " ".join(argv[3:])

    exit_code = _parse_exit_code(errorlevel)
    succeeded = exit_code == 0

    cfg = load()
    _configure_file_logging(cfg.debug_log)
    for warning in cfg.warnings:
        LOGGER.warning("%s", warning)

    if succeeded and not cfg.on_success:
        LOGGER.debug("job %s succeeded and on_success is off; nothing to send", jobid)
        return
    if not succeeded and not cfg.on_failure:
        LOGGER.debug("job %s failed and on_failure is off; nothing to send", jobid)
        return

    duration = None
    if cfg.duration:
        duration = render.format_duration(tsquery.job_duration(cfg.ts_command, jobid))

    excerpt: str | None = None
    detected: str | None = None
    source: Path | None = None
    complete = True
    if not succeeded:
        excerpt, detected, source, complete = _collect_log(cfg, output_file)

    fields = render.Fields(
        jobid=jobid,
        command=command,
        exit_code=exit_code,
        status="success" if succeeded else "failure",
        host=socket.gethostname(),
        output_file=output_file,
        label=os.environ.get("TS_BOT_LABEL") or None,
        duration=duration,
        detected=detected,
    )

    template = cfg.success_template if succeeded else cfg.failure_template
    message, truncated = render.render(template, fields, excerpt)

    telegram.send_message(
        api_base_url=cfg.api_base_url,
        token=cfg.token,
        chat_id=cfg.chat_id,
        text=message,
        timeout=cfg.timeout_seconds,
    )
    LOGGER.debug("sent %s notification for job %s", fields.status, jobid)

    # Attach the log whenever the message does not already show all of it.
    if source is not None and cfg.attach_full_log and (truncated or not complete):
        _attach_log(cfg, jobid, source)


def _parse_exit_code(errorlevel: str) -> int:
    try:
        return int(errorlevel)
    except ValueError:
        # Treat an unreadable errorlevel as a failure: a false alarm beats
        # silently swallowing a broken job.
        LOGGER.warning("unparseable errorlevel %r; assuming failure", errorlevel)
        return -1


def _collect_log(
    cfg: Config, output_file: str
) -> tuple[str | None, str | None, Path | None, bool]:
    """Return ``(excerpt, detected_name, source_path, shows_whole_log)``."""
    source = logscan.error_source(output_file)
    if source is None:
        LOGGER.debug("no readable output file at %s", output_file)
        return None, None, None, True

    text = logscan.read_tail(source, cfg.tail_bytes)
    if text is None:
        LOGGER.warning("could not read output file %s", source)
        return None, None, source, True

    excerpt: str | None = None
    detected: str | None = None

    if cfg.extract_errors:
        found = logscan.extract_error(text, cfg.tail_lines)
        if found is not None:
            excerpt, detected = found

    if excerpt is None:
        excerpt = logscan.last_lines(text, cfg.tail_lines)

    enough = _shows_enough(source, cfg.tail_bytes, text, excerpt)
    return excerpt, detected, source, enough


def _shows_enough(source: Path, tail_bytes: int, text: str, excerpt: str) -> bool:
    """True when the message covers enough of the log to skip the attachment.

    Uploading a file for every failure is clutter, so the bar is "there is more
    than another message's worth of log you have not seen" -- either because
    the file outgrew the tail we read, or because extraction skipped a
    substantial prefix.
    """
    try:
        if source.stat().st_size > tail_bytes:
            return False
    except OSError:
        return False

    unseen = len(text.strip()) - len(excerpt.strip())
    return unseen <= render.TELEGRAM_MAX_CHARS


def _attach_log(cfg: Config, jobid: str, source: Path) -> None:
    try:
        content = telegram.read_attachment(source, cfg.max_attachment_bytes)
    except OSError as exc:
        LOGGER.warning("could not read %s for upload: %s", source, exc)
        return

    if not content:
        return

    telegram.send_document(
        api_base_url=cfg.api_base_url,
        token=cfg.token,
        chat_id=cfg.chat_id,
        filename=telegram.attachment_name(jobid, source, cfg.max_attachment_bytes),
        content=content,
        timeout=cfg.timeout_seconds,
    )
    LOGGER.debug("attached log for job %s (%d bytes)", jobid, len(content))


def _configure_stderr_logging() -> None:
    """Silent by default; TS_BOT_DEBUG=1 makes the hook explain itself."""
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()  # idempotent, so repeated calls do not stack handlers
    LOGGER.addHandler(logging.NullHandler())

    if os.environ.get("TS_BOT_DEBUG"):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("ts-bot: %(levelname)s: %(message)s"))
        LOGGER.addHandler(handler)


def _configure_file_logging(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as exc:
        LOGGER.warning("cannot open debug log %s: %s", path, exc)
        return

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    LOGGER.addHandler(handler)
