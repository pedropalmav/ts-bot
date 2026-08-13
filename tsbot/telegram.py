"""Minimal Telegram Bot API client built on the standard library.

No third-party dependency on purpose: this code runs as a task-spooler hook,
and the job's slot stays occupied for as long as the hook lives. Importing a
full async bot framework to send one message would cost more startup time than
the request itself.
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# Bots may upload files up to 50 MB. max_attachment_bytes should stay well
# under this; the check here is a backstop.
TELEGRAM_MAX_UPLOAD_BYTES = 50 * 1024 * 1024


class TelegramError(Exception):
    """A request failed. Caught by the CLI, logged, never propagated."""


def send_message(
    *,
    api_base_url: str,
    token: str,
    chat_id: str,
    text: str,
    timeout: float,
    parse_mode: str = "HTML",
) -> dict:
    body = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        _endpoint(api_base_url, token, "sendMessage"),
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    return _perform(request, timeout)


def send_document(
    *,
    api_base_url: str,
    token: str,
    chat_id: str,
    filename: str,
    content: bytes,
    timeout: float,
) -> dict:
    """Upload ``content`` as a document.

    Sent as its own request *after* the text message and with no caption, which
    sidesteps the 1024-character caption limit entirely.
    """
    if len(content) > TELEGRAM_MAX_UPLOAD_BYTES:
        raise TelegramError(
            f"attachment is {len(content)} bytes, over Telegram's 50 MB bot limit"
        )

    boundary = f"----tsbot{secrets.token_hex(16)}"
    body = _multipart_body(
        boundary,
        fields={"chat_id": chat_id, "disable_notification": "true"},
        filename=filename,
        content=content,
    )

    request = urllib.request.Request(
        _endpoint(api_base_url, token, "sendDocument"),
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    return _perform(request, timeout)


def read_attachment(path: str | Path, max_bytes: int) -> bytes:
    """Read the trailing ``max_bytes`` of a file for upload."""
    path = Path(path)
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        return handle.read()


def attachment_name(jobid: str, path: str | Path, max_bytes: int) -> str:
    """Name the upload so a partial log is self-evidently partial."""
    try:
        complete = Path(path).stat().st_size <= max_bytes
    except OSError:
        complete = False
    return f"job-{jobid}.log" if complete else f"job-{jobid}.tail.log"


def _endpoint(api_base_url: str, token: str, method: str) -> str:
    return f"{api_base_url.rstrip('/')}/bot{token}/{method}"


def _multipart_body(
    boundary: str, fields: dict[str, str], filename: str, content: bytes
) -> bytes:
    content_type = mimetypes.guess_type(filename)[0] or "text/plain"
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))

    return b"".join(parts)


def _perform(request: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Telegram's 4xx bodies explain the problem precisely -- surface them.
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise TelegramError(f"HTTP {exc.code} from Telegram: {detail}") from None
    except urllib.error.URLError as exc:
        raise TelegramError(f"cannot reach Telegram: {exc.reason}") from None
    except (TimeoutError, OSError) as exc:
        raise TelegramError(f"request failed: {exc}") from None
    except json.JSONDecodeError as exc:
        raise TelegramError(f"malformed response from Telegram: {exc}") from None

    if not payload.get("ok", False):
        raise TelegramError(f"Telegram rejected the request: {payload}")

    return payload
