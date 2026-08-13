# ts-bot

Telegram notifications for [task-spooler](https://viric.name/soft/ts/) jobs.

Queue a job on a server, walk away, and get a message when it finishes:

> ✅ **Job 43** finished cleanly · 3m12s
> `python train.py --epochs 100`

and when it doesn't:

> ❌ **[train-v3] Job 42** failed — exit 1 · 3m12s
> `python train.py --epochs 100`
> *detected: RuntimeError*
> ```
> Traceback (most recent call last):
>   File "train.py", line 61, in main
>     loss.backward()
> RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB
> ```
> 📎 job-42.log

Successes stay short on purpose. Failures carry the job id and the part of the
console output that explains the failure.

## Requirements

- Python 3.11+ (3.9/3.10 also work with `pip install tomli`)
- task-spooler with `TS_ONFINISH` support (both viric's original and
  justanhduc's GPU fork qualify)
- **No third-party packages.** Standard library only.

## Setup

**1. Create the bot.** Message [@BotFather](https://t.me/BotFather), send
`/newbot`, and keep the token it gives you.

**2. Find your chat id.** Send your new bot any message, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"][0]["message"]["chat"]["id"])'
```

The bot cannot message you until you have written to it first — that is a
Telegram rule, not a limitation here.

**3. Install on the server.**

```bash
git clone <this-repo> ~/ts-bot
chmod +x ~/ts-bot/ts-hook

mkdir -p ~/.config/ts-bot
cp ~/ts-bot/config.example.toml ~/.config/ts-bot/config.toml
chmod 600 ~/.config/ts-bot/config.toml   # it holds your bot token
```

Fill in `token` and `chat_id`. If you would rather keep secrets out of files,
leave them empty and export `TS_BOT_TOKEN` / `TS_BOT_CHAT_ID` instead — the
environment takes precedence.

On Debian/Ubuntu the binary is `tsp`, not `ts`; set `ts_command = "tsp"` in the
config so duration lookups work.

**4. Enable the hook.** In `~/.bashrc`:

```bash
export TS_ONFINISH=$HOME/ts-bot/ts-hook
```

> **This must be exported before you enqueue anything.** task-spooler runs jobs
> from the client process, not a daemon, so the hook is read from the
> environment of the shell that ran `ts`. Jobs queued before you exported it
> will not notify. Re-login or `source ~/.bashrc`, then queue as usual:

```bash
ts python train.py --epochs 100
```

**5. Check it works.**

```bash
ts bash -c 'echo fine; exit 0'                    # expect a success message
ts bash -c 'python3 -c "raise RuntimeError(1)"'   # expect a failure + traceback
```

## Configuration

Everything lives in `~/.config/ts-bot/config.toml`; see
[`config.example.toml`](config.example.toml) for the annotated version.

| Key | Default | Notes |
|---|---|---|
| `notify.on_success` | `true` | Set `false` for failure-only mode |
| `notify.duration` | `true` | Looked up via `ts -i`; set `false` to skip the subprocess |
| `notify.ts_command` | `"ts"` | `"tsp"` on Debian/Ubuntu |
| `log.tail_bytes` | `65536` | How much of the output file to read, from the end |
| `log.tail_lines` | `30` | Cap on lines shown inline |
| `log.extract_errors` | `true` | Scan for a traceback / `ERROR` marker instead of a blind tail |
| `log.attach_full_log` | `true` | Upload the log when the message can't show it all |
| `telegram.timeout_seconds` | `10` | Keep this tight — see *Design notes* |

### Per-job control

```bash
TS_BOT_LABEL=train-v3 ts python train.py    # tags the notification "[train-v3]"
```

### Message templates

`[templates].success` and `[templates].failure` are format strings rendered with
`parse_mode=HTML`, so `<b>`, `<i>`, `<code>` and `<pre>` work. Interpolated
values are HTML-escaped for you.

| Placeholder | |
|---|---|
| `{jobid}` | task-spooler job id |
| `{command}` | the queued command |
| `{exit_code}` | `exit 3`, or `killed or crashed (signal)` for `-1` |
| `{status}` | `success` / `failure` |
| `{label}` | `[name] ` when `TS_BOT_LABEL` is set, else empty |
| `{duration}` | ` · 3m12s` when known, else empty |
| `{detected}` | the `detected: RuntimeError` line, when identified |
| `{log}` | the error excerpt (failure template only) |
| `{host}` | server hostname |
| `{output_file}` | path to task-spooler's output file |

`{label}`, `{duration}` and `{detected}` carry their own separators, so one
template reads correctly whether or not they are set. A placeholder that doesn't
exist is left visible in the message rather than suppressing it.

## Design notes

The hook is deliberately small, because of how task-spooler calls it.

`hook_on_finish()` (`mail.c:79`) does `fork()` + `execlp()` and then `wait()`s,
and it is called from `execute.c:86` — **before** `c_end_of_job()`. The job's
slot therefore stays occupied for as long as the hook runs. Two consequences
shaped everything here:

- **The hook must never hang.** Every network call and subprocess has an
  explicit timeout, with `socket.setdefaulttimeout()` as a backstop. Measured
  overhead is ~60 ms plus one HTTPS round trip.
- **The hook must never crash.** A traceback would land in the stderr of the
  shell that queued the job. `main()` catches everything and always exits 0.

It also means the hook cannot call `ts -c <id>` to fetch output — at that moment
the job is not yet marked finished and the call would block. It reads the output
file it is handed instead, seeking to the tail so a multi-gigabyte training log
is never loaded into memory.

Messages use HTML rather than MarkdownV2: MarkdownV2 requires escaping
``_ * [ ] ( ) ~ ` > # + - = | { } . !``, all of which occur in ordinary stack
traces, and one missed character means the API rejects the whole message.

## Troubleshooting

Nothing arrives? The hook is silent by design. Make it talk:

```bash
TS_BOT_DEBUG=1 ~/ts-bot/ts-hook 42 1 /tmp/some.log "test command"
```

This runs the hook exactly the way task-spooler does — four positional
arguments — and prints what it decided and any error. For ongoing diagnosis, set
`debug.log_file` in the config.

Common causes:

- `TS_ONFINISH` wasn't exported when the job was queued (check `ts -i <id>`).
- `ts-hook` isn't executable (`chmod +x`).
- You never messaged the bot, so Telegram refuses to deliver to your chat.
- Wrong `chat_id` — Telegram returns a descriptive 400, visible with `TS_BOT_DEBUG=1`.

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```

`tests/test_cli.py` runs the whole hook against a local `http.server` standing
in for the Telegram API, so the real request-building code is exercised without
touching the network.

## Limitations

- A notification is lost if Telegram is unreachable when the job ends. The hook
  records this in the debug log but does not retry — retrying would hold the
  job's slot open.
- No interactive commands (`/queue`, `/log`). This is a notifier, not a daemon.
- `{duration}` is best-effort and renders empty when `ts -i` output can't be
  parsed.
