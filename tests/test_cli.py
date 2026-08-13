"""End-to-end tests driving the hook exactly as task-spooler does.

A local http.server stands in for api.telegram.org via the `api_base_url`
config knob, so these exercise the real urllib request-building code.
"""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tsbot.cli import main

TRACEBACK_LOG = """\
epoch 1 loss 0.4
Traceback (most recent call last):
  File "train.py", line 61, in main
    loss.backward()
RuntimeError: CUDA out of memory.
"""

TOKEN = "123456:TESTTOKEN"


class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type", ""),
            }
        )

        status = self.server.status
        payload = json.dumps({"ok": status == 200, "result": {}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def stub():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.requests = []
    server.status = 200
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def write_config(tmp_path, monkeypatch, stub):
    def _write(**overrides):
        settings = {
            "on_success": "true",
            "on_failure": "true",
            "extract_errors": "true",
            "attach_full_log": "true",
            "tail_bytes": "65536",
            "max_attachment_bytes": "5242880",
        }
        settings.update({k: str(v) for k, v in overrides.items()})

        path = tmp_path / "config.toml"
        path.write_text(
            f"""
[telegram]
token = "{TOKEN}"
chat_id = "999"
timeout_seconds = 5
api_base_url = "{stub.base_url}"

[notify]
on_success = {settings["on_success"]}
on_failure = {settings["on_failure"]}
duration = false

[log]
tail_bytes = {settings["tail_bytes"]}
tail_lines = 30
extract_errors = {settings["extract_errors"]}
attach_full_log = {settings["attach_full_log"]}
max_attachment_bytes = {settings["max_attachment_bytes"]}
"""
        )
        path.chmod(0o600)
        monkeypatch.setenv("TS_BOT_CONFIG", str(path))
        return path

    return _write


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("TS_BOT_TOKEN", "TS_BOT_CHAT_ID", "TS_BOT_LABEL", "TS_BOT_DEBUG"):
        monkeypatch.delenv(name, raising=False)


def form(request):
    return {k: v[0] for k, v in urllib.parse.parse_qs(request["body"].decode()).items()}


def messages(stub):
    return [r for r in stub.requests if r["path"].endswith("/sendMessage")]


def documents(stub):
    return [r for r in stub.requests if r["path"].endswith("/sendDocument")]


def test_success_sends_a_short_message_and_no_attachment(stub, write_config, tmp_path):
    write_config()
    log = tmp_path / "out"
    log.write_text("all good\n")

    assert main(["42", "0", str(log), "echo hello"]) == 0

    assert len(messages(stub)) == 1
    assert documents(stub) == []

    request = messages(stub)[0]
    assert request["path"] == f"/bot{TOKEN}/sendMessage"

    payload = form(request)
    assert payload["chat_id"] == "999"
    assert payload["parse_mode"] == "HTML"
    assert "finished cleanly" in payload["text"]
    assert "Job <b>42</b>" in payload["text"]
    # A success message must not leak the job's console output.
    assert "all good" not in payload["text"]


def test_failure_includes_the_traceback(stub, write_config, tmp_path):
    write_config()
    log = tmp_path / "out"
    log.write_text(TRACEBACK_LOG)

    assert main(["42", "1", str(log), "python train.py"]) == 0

    payload = form(messages(stub)[0])
    assert "failed" in payload["text"]
    assert "exit 1" in payload["text"]
    assert "detected: RuntimeError" in payload["text"]
    assert "RuntimeError: CUDA out of memory." in payload["text"]
    # Extraction dropped the noise above the traceback.
    assert "epoch 1 loss" not in payload["text"]


def test_signal_death_is_not_reported_as_exit_minus_one(stub, write_config, tmp_path):
    write_config()
    log = tmp_path / "out"
    log.write_text("killed\n")

    main(["7", "-1", str(log), "sleep 999"])

    assert "killed or crashed (signal)" in form(messages(stub)[0])["text"]


def test_large_log_is_attached_as_a_document(stub, write_config, tmp_path):
    write_config(tail_bytes=2048)
    log = tmp_path / "out"
    log.write_text("\n".join(f"line {i:05d}" for i in range(5000)) + "\n")

    main(["42", "1", str(log), "make"])

    assert len(messages(stub)) == 1
    assert len(documents(stub)) == 1

    upload = documents(stub)[0]
    assert upload["content_type"].startswith("multipart/form-data; boundary=")
    assert b'name="chat_id"' in upload["body"]
    # The whole file fits under max_attachment_bytes, so it is uploaded intact.
    assert b'filename="job-42.log"' in upload["body"]
    assert b"line 00000" in upload["body"]
    assert b"line 04999" in upload["body"]


def test_oversized_attachment_is_named_as_a_tail(stub, write_config, tmp_path):
    write_config(tail_bytes=2048, max_attachment_bytes=4096)
    log = tmp_path / "out"
    log.write_text("\n".join(f"line {i:05d}" for i in range(5000)) + "\n")

    main(["42", "1", str(log), "make"])

    upload = documents(stub)[0]
    assert b'filename="job-42.tail.log"' in upload["body"]
    # Only the tail travelled; the head was left behind.
    assert b"line 04999" in upload["body"]
    assert b"line 00000" not in upload["body"]


def test_small_log_shown_inline_is_not_also_attached(stub, write_config, tmp_path):
    """Extraction dropping a line of preamble must not trigger an upload."""
    write_config()
    log = tmp_path / "out"
    log.write_text(TRACEBACK_LOG)

    main(["42", "1", str(log), "python train.py"])

    assert len(messages(stub)) == 1
    assert documents(stub) == []


def test_attachment_can_be_disabled(stub, write_config, tmp_path):
    write_config(tail_bytes=512, attach_full_log="false")
    log = tmp_path / "out"
    log.write_text("\n".join(f"line {i:05d}" for i in range(5000)) + "\n")

    main(["42", "1", str(log), "make"])

    assert len(messages(stub)) == 1
    assert documents(stub) == []


def test_failure_only_mode_stays_quiet_on_success(stub, write_config, tmp_path):
    write_config(on_success="false")
    log = tmp_path / "out"
    log.write_text("fine\n")

    assert main(["42", "0", str(log), "echo hello"]) == 0
    assert stub.requests == []

    # ...but still reports failures.
    assert main(["43", "1", str(log), "echo hello"]) == 0
    assert len(messages(stub)) == 1


def test_label_from_the_environment_appears_in_the_message(
    stub, write_config, tmp_path, monkeypatch
):
    write_config()
    monkeypatch.setenv("TS_BOT_LABEL", "train-v3")
    log = tmp_path / "out"
    log.write_text("ok\n")

    main(["42", "0", str(log), "python train.py"])

    assert "[train-v3] Job <b>42</b>" in form(messages(stub)[0])["text"]


def test_separate_stderr_file_is_preferred(stub, write_config, tmp_path):
    write_config()
    stdout = tmp_path / "out"
    stdout.write_text("progress bar noise\n")
    (tmp_path / "out.e").write_text(TRACEBACK_LOG)

    main(["42", "1", str(stdout), "python train.py"])

    text = form(messages(stub)[0])["text"]
    assert "RuntimeError: CUDA out of memory." in text
    assert "progress bar noise" not in text


def test_missing_output_file_still_notifies(stub, write_config, tmp_path):
    # Job queued with `ts -n`: no output was stored.
    write_config()

    assert main(["42", "1", str(tmp_path / "gone"), "make"]) == 0

    text = form(messages(stub)[0])["text"]
    assert "failed" in text


# --- the never-crash boundary -------------------------------------------------


def test_server_error_is_swallowed(stub, write_config, tmp_path):
    write_config()
    stub.status = 500
    log = tmp_path / "out"
    log.write_text("ok\n")

    assert main(["42", "0", str(log), "echo hello"]) == 0


def test_missing_config_is_swallowed(monkeypatch, tmp_path):
    monkeypatch.setenv("TS_BOT_CONFIG", str(tmp_path / "absent.toml"))

    assert main(["42", "0", "/tmp/whatever", "echo hello"]) == 0


def test_too_few_arguments_is_swallowed(stub, write_config):
    write_config()

    assert main(["42", "0"]) == 0
    assert stub.requests == []


def test_unparseable_errorlevel_is_treated_as_failure(stub, write_config, tmp_path):
    write_config()
    log = tmp_path / "out"
    log.write_text("ok\n")

    assert main(["42", "banana", str(log), "make"]) == 0
    assert "failed" in form(messages(stub)[0])["text"]


def test_unreachable_server_is_swallowed(write_config, tmp_path, monkeypatch):
    write_config()
    # Rewrite the config to point at a port nothing is listening on.
    config = tmp_path / "config.toml"
    config.write_text(
        config.read_text().replace(
            config.read_text().split('api_base_url = "')[1].split('"')[0],
            "http://127.0.0.1:1",
        )
    )
    log = tmp_path / "out"
    log.write_text("ok\n")

    assert main(["42", "0", str(log), "echo hello"]) == 0
