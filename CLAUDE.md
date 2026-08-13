# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A `TS_ONFINISH` hook for [task-spooler](https://viric.name/soft/ts/) that sends a
Telegram message when a queued job finishes: terse on success, job id + extracted
error log on failure. **Standard library only** — adding a third-party runtime
dependency defeats the point (it installs with `git clone`, on servers where the
user may not control the Python environment).

## Commands

```bash
# Tests (pytest lives only in the venv; it is not a runtime dependency)
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_cli.py::test_failure_includes_the_traceback -q

# Syntax check both the package and the non-.py entry point
python3 -m compileall -q tsbot ts-hook

# Exercise the hook exactly as task-spooler does — four positional args
TS_BOT_DEBUG=1 ./ts-hook 42 1 /tmp/some.log "python train.py"
```

There is no build step, no linter config, and no packaging metadata.

## The constraint everything else follows from

task-spooler's `hook_on_finish()` (`mail.c:79`) does `fork()` + `execlp()` and
then `wait()`s, and it is called from `execute.c:86` — **before**
`c_end_of_job()`. The job's slot stays occupied for the hook's entire wall-clock
life. Three rules fall out, and breaking any of them degrades the user's queue
rather than just this tool:

1. **Never hang.** Every network call and subprocess has an explicit timeout,
   with `socket.setdefaulttimeout()` in `cli.main()` as a backstop. No retries —
   a retry loop holds the slot open. Budget is ~60 ms plus one HTTPS round trip.
2. **Never crash.** `cli.main()` catches `BaseException` and always returns 0; a
   traceback would land in the stderr of whatever shell queued the job. This is
   why `config.load()` raises `ConfigError` with a readable message instead of
   letting exceptions escape, and why `tsquery` returns `None` on every failure
   mode.
3. **Never call `ts -c <id>`.** At hook time the job is not yet marked finished,
   so that call would deadlock. The hook reads the output file path it was
   handed instead.

The hook also inherits the **enqueuing shell's** environment (task-spooler runs
jobs from the client, not a daemon), which is why `TS_BOT_LABEL` works and why
`TS_ONFINISH` must be exported before the job is queued.

## Architecture

`ts-hook` (chmod +x, no `.py`) is a shim that `sys.path.insert`s the repo root
and calls `tsbot.cli.main()`. It must remain a real executable file — `execlp`
resolves it as a program. All logic lives in `tsbot/` so it stays importable and
unit-testable.

`cli._run()` is the only orchestration: parse argv → load config → apply the
`on_success`/`on_failure` filter → look up duration → collect the log (failures
only) → render → send → maybe attach. The other modules are pure-ish and have no
knowledge of each other.

- **`config.py`** — the user's entire control surface. Env wins over file
  (`TS_BOT_TOKEN`, `TS_BOT_CHAT_ID`, `TS_BOT_CONFIG`). Note `Config.warnings`: a
  tuple the CLI emits after logging is wired up, because `load()` cannot log its
  own warnings — it is what tells us where to log.
- **`logscan.py`** — `error_source()` prefers `<ofname>.e` (where `ts -E` puts
  stderr, a path the hook is never told about) over the main output file.
  `read_tail()` seeks to the tail and sniffs gzip magic, so a multi-GB training
  log is never fully read. `extract_error()` finds the *last* traceback, falling
  back to an `ERROR`/`FATAL` window, then to `None` so the caller uses a plain
  tail.
- **`render.py`** — `parse_mode=HTML`, never MarkdownV2: MarkdownV2 requires
  escaping `` _ * [ ] ( ) ~ ` > # + - = | { } . ! ``, all of which occur in
  ordinary stack traces, and one miss is an HTTP 400 for the whole message.
  Two subtleties worth preserving: `_fit_log` trims **raw** lines and escapes
  afterwards (slicing already-escaped text can cut `&amp;` in half), and
  `_SafeDict.__missing__` leaves an unknown placeholder visible rather than
  raising — a template typo should produce an odd message, not a lost
  notification.
- **`telegram.py`** — hand-rolled `multipart/form-data` over `urllib`. The
  document is a separate request *after* the text with no caption, which
  sidesteps Telegram's 1024-char caption cap (messages get the full 4096).
- **`tsquery.py`** — duration via `ts -i <jobid>`, because the hook is given no
  duration and `st_birthtime` does not exist on Linux. `notify.ts_command`
  exists because Debian/Ubuntu ship the binary as `tsp`.

### Conventions that are load-bearing

- `{label}`, `{duration}` and `{detected}` are **pre-decorated** in
  `render._escaped_values` (`"[x] "`, `" · 3m12s"`, `"<i>detected: X</i>\n"`) or
  empty, so a single template reads correctly whether or not they are set. Any
  new optional placeholder should carry its own separator the same way.
- `errorlevel == -1` means died-by-signal, not exit status -1; see
  `format_exit_code`. An unparseable errorlevel is treated as failure — a false
  alarm beats swallowing a broken job.
- Success messages must never include console output. `test_cli.py` asserts this.
- `cli._shows_enough()` decides whether to also upload the log. The rule is
  "more than another message's worth of log went unseen" — an earlier
  "excerpt != whole file" rule attached a document to nearly every failure,
  because extraction legitimately drops preamble.

## Tests

`tests/test_cli.py` runs the real hook end to end against a local `http.server`
via the `telegram.api_base_url` config knob — that knob exists for the tests —
so the actual urllib request-building and multipart encoding are exercised
without network access. Prefer adding cases there over mocking `telegram.py`.

Unit tests import `tsbot.cli` directly and therefore cannot catch a broken
shebang, a bad `sys.path` insert, or a lost executable bit. Changes to `ts-hook`
need a subprocess smoke test.

Config changes should also be checked against `config.example.toml` — the file
users actually copy. The suite uses the built-in defaults from `config.py` and
will not notice if the example drifts.
