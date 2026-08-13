"""Configuration loading.

The hook runs inside whatever shell enqueued the job, so nothing here may
assume more about the environment than ``$HOME``.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - depends on the host
        tomllib = None  # type: ignore[assignment]


class ConfigError(Exception):
    """Config is missing or unusable. Always caught by the CLI's never-crash boundary."""


# Rendered with parse_mode=HTML, so only & < > need escaping in interpolated
# values -- see render.py for why MarkdownV2 is not an option here.
#
# {label}, {duration} and {detected} arrive pre-decorated (with their trailing
# separator, or empty) so a single template works whether or not they are set.
DEFAULT_SUCCESS_TEMPLATE = (
    "✅ {label}Job <b>{jobid}</b> finished cleanly{duration}\n"
    "<code>{command}</code>"
)

DEFAULT_FAILURE_TEMPLATE = (
    "❌ {label}Job <b>{jobid}</b> failed — {exit_code}{duration}\n"
    "<code>{command}</code>\n"
    "{detected}"
    "<pre>{log}</pre>"
)


@dataclass(frozen=True)
class Config:
    token: str
    chat_id: str
    timeout_seconds: float
    api_base_url: str

    on_success: bool
    on_failure: bool
    duration: bool
    ts_command: str

    tail_lines: int
    tail_bytes: int
    extract_errors: bool
    attach_full_log: bool
    max_attachment_bytes: int

    success_template: str
    failure_template: str

    debug_log: Path | None

    # Collected during load() and emitted by the CLI once logging is wired up.
    # load() cannot log its own warnings: it is what tells us where to log.
    warnings: tuple[str, ...] = field(default=())


def config_path() -> Path:
    """Resolve the config location: $TS_BOT_CONFIG, then XDG, then ~/.config."""
    explicit = os.environ.get("TS_BOT_CONFIG")
    if explicit:
        return Path(explicit).expanduser()

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "ts-bot" / "config.toml"


def load(path: Path | None = None) -> Config:
    """Read and validate the config file.

    Raises ConfigError with an actionable message rather than letting a
    traceback escape into the enqueuing shell's stderr.
    """
    if tomllib is None:  # pragma: no cover - depends on the host
        raise ConfigError(
            "no TOML parser available: use Python 3.11+, or `pip install tomli`"
        )

    path = path or config_path()
    warnings: list[str] = []

    try:
        raw_bytes = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(
            f"config not found at {path}; copy config.example.toml there"
        ) from None
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from None

    # The file holds a bot token; anyone who can read it can post as the bot.
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            warnings.append(f"{path} is group/world accessible; run: chmod 600 {path}")
    except OSError:
        pass

    try:
        data: dict[str, Any] = tomllib.loads(raw_bytes.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot parse {path}: {exc}") from None

    telegram = _section(data, "telegram")
    notify = _section(data, "notify")
    log = _section(data, "log")
    templates = _section(data, "templates")
    debug = _section(data, "debug")

    # Env wins, so the token can stay out of files entirely.
    token = os.environ.get("TS_BOT_TOKEN") or str(telegram.get("token", "")).strip()
    chat_id = os.environ.get("TS_BOT_CHAT_ID") or str(telegram.get("chat_id", "")).strip()

    if not token:
        raise ConfigError("no bot token: set telegram.token or $TS_BOT_TOKEN")
    if not chat_id:
        raise ConfigError("no chat id: set telegram.chat_id or $TS_BOT_CHAT_ID")

    debug_log_raw = str(debug.get("log_file", "")).strip()

    return Config(
        token=token,
        chat_id=chat_id,
        timeout_seconds=float(telegram.get("timeout_seconds", 10)),
        api_base_url=str(
            telegram.get("api_base_url", "https://api.telegram.org")
        ).rstrip("/"),
        on_success=bool(notify.get("on_success", True)),
        on_failure=bool(notify.get("on_failure", True)),
        duration=bool(notify.get("duration", True)),
        ts_command=str(notify.get("ts_command", "ts")),
        tail_lines=int(log.get("tail_lines", 30)),
        tail_bytes=int(log.get("tail_bytes", 65536)),
        extract_errors=bool(log.get("extract_errors", True)),
        attach_full_log=bool(log.get("attach_full_log", True)),
        max_attachment_bytes=int(log.get("max_attachment_bytes", 5 * 1024 * 1024)),
        success_template=str(templates.get("success") or DEFAULT_SUCCESS_TEMPLATE),
        failure_template=str(templates.get("failure") or DEFAULT_FAILURE_TEMPLATE),
        debug_log=Path(debug_log_raw).expanduser() if debug_log_raw else None,
        warnings=tuple(warnings),
    )


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    return value if isinstance(value, dict) else {}
