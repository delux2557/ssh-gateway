# SSH Gateway

Turn one SSH-only machine into an HTTP API.

You have a box you can only reach with SSH — a container with no agent, a router,
an embedded board, a CI worker, a recovery image. This is a small Python service
that holds one persistent SSH transport to it and exposes the things you
actually want to do over HTTP: run a command, watch its output as it happens,
start a job that outlives the request, and move directory trees in or out.

Every reply is one JSON envelope, so it is trivial to call from a script, a
build step, or an LLM agent's tool layer.

```json
{ "ok": true, "data": { "...": "..." }, "error": "", "code": "" }
```

## What it is for

| Situation | Endpoint |
| --- | --- |
| Run a command, wait for the result | `POST /run` |
| Watch a long command's output live | `POST /run/stream` (SSE) |
| Start something that must survive the request | `POST /run/async` |
| Follow or kill that job later | `GET /job/{id}/output?follow=1`, `POST /job/{id}/kill` |
| Mirror a directory up or down, incrementally | `POST /sftp/sync` |
| Ask what the gateway can currently do | `GET /health`, `GET /openapi.json` |

## Install

```bash
pip install ssh-gateway          # or: pip install -e .
```

Python 3.9+. The only runtime dependency is `paramiko`.

## Quick start

Configuration is environment-only, prefixed `SSHGW_`. Nothing about a specific
device is baked in, and the gateway refuses to start rather than guess a target.

```bash
export SSHGW_TARGET=deploy@example.internal:22
export SSHGW_REMOTE_KEY=~/.ssh/id_ed25519      # or SSHGW_REMOTE_PASS
export SSHGW_TOKEN=$(openssl rand -hex 16)     # require Bearer auth
ssh-gateway
```

```console
$ curl -s -X POST localhost:8023/run \
    -H "Authorization: Bearer $SSHGW_TOKEN" \
    -d '{"cmd":"uname -a; uptime"}'
{"ok": true, "data": {"cmd": "uname -a; uptime", "stdout": "...", "stderr": "",
 "exit_code": 0, "duration_ms": 41}, "error": "", "code": ""}
```

Without a key or password flag on the command line: `ssh-gateway --ask-pass`
prompts instead. A `--password` flag is deliberately absent — command-line
arguments are readable by every process on the machine.

## The `gw` client

`gw` is a stdlib-only CLI shipped with the package. Its one trick: the command
is sent as a JSON *string value*, so it never round-trips through your local
shell — `$HOME`, quotes and pipes arrive at the remote shell exactly as typed.

```bash
export GW_URL=http://localhost:8023 GW_TOKEN=...

gw run 'echo $HOME'                 # expanded remotely, untouched locally
gw stream 'tail -n 200 -f /var/log/syslog'
gw async 'make -j4'                 # prints a job id
gw output <job-id> --follow
gw jobs
gw sftp put ./build /srv/build -r
gw sftp sync --direction=pull --delete /var/log ./logs
gw status
```

Every verb accepts `--json`, on either side of the verb (`gw --json status` and
`gw status --json` are the same call), and prints the response's `data` payload
as indented JSON instead of formatted text — that is the shape a non-human
caller should parse.

One consequence is worth knowing before you build on it: **`--json` always exits
`0`.** The outcome you care about is a field inside that payload, not `gw`'s own
status. Without `--json`, `gw run` exits with the remote command's exit code
(`gw run 'exit 7'` → `7`); with it, the same call exits `0` and `exit_code: 7`
sits in the JSON.

## Recipes

The patterns worth copying when a script or an agent drives the gateway. Every
one below was executed against a real target — a Linux box reachable only over
SSH, `bash`, no busybox — before being written down, and two of them were wrong
on the first attempt. The corrected versions are here, with the failure left in,
because it is the failure you will hit.

### Ask what the target can do before asking it to do something

Most remote-script failures are an assumption, not a bug. One round trip retires
them:

```bash
gw run 'for c in bash sh busybox; do printf "%-8s %s\n" "$c" "$(command -v "$c" || echo -)"; done
        for f in /proc/loadavg /proc/uptime /proc/meminfo; do
          [ -r "$f" ] && echo "READ  $f" || echo "DENY  $f"
        done'
```

What came back on the target used here:

| Probe | Result |
| --- | --- |
| `bash`, `sh` | both present — compound commands can be written normally |
| `busybox` | absent |
| `/proc/loadavg`, `/proc/uptime` | present, but **denied** to this account |
| `/proc/meminfo` | readable |

A file existing is not a file being readable, so probe per file — and let
`command -v`, not a guess, answer whether a shell is there.

### Degrade instead of failing

Given that table, a load-average recipe has to carry its own fallback: `uptime`
prints the same three numbers through a different door.

```bash
gw run 'if [ -r /proc/loadavg ]; then cat /proc/loadavg; else uptime; fi'
```

### Remote paths do pass through your local shell — for SFTP only

The gateway's promise is that a command never round-trips through your local
shell: it travels as a JSON *string value*. That holds for `/run`. It does
**not** hold for SFTP paths, which are positional argv — and on Windows,
Git-Bash rewrites any argument that looks like an absolute POSIX path before
Python ever sees it:

```console
$ gw sftp ls /srv/backups
gw: HTTP 404 NOT_FOUND: remote path does not exist:
    C:/programs/git/srv/backups
```

The remote side answered honestly; it was simply asked about a different path.
Two ways out, both measured:

```bash
MSYS_NO_PATHCONV=1 gw sftp ls /srv/backups    # or:
gw sftp ls //srv/backups                      # a doubled leading slash survives
```

`~` is not expanded remotely either: `gw sftp ls '~'` looks for a directory
literally named `~`. Spell the path out.

### A snapshot that explains itself

Collect on the target, describe the collection *on the target*, ship one
tarball, pull it back. Keep stdout for the single thing you need to capture and
let everything else pass through as narration:

```bash
SNAP=$(gw run 'base="$HOME"; d="$base/snap-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$d"
if [ -r /proc/loadavg ]; then L=$(cat /proc/loadavg); else L=$(uptime | sed "s/.*average: *//"); fi
{
  echo "target : $(uname -srm)"
  echo "user   : $(whoami)"
  echo "when   : $(date)"
  echo "load   : $L"
  echo "---"
  echo "uptime.txt  uptime(1); readable even where /proc/loadavg is not"
  echo "disk.txt    df -h \$HOME"
  echo "mem.txt     MemTotal/MemFree, or a DENIED marker"
} > "$d/_README.txt"
uptime > "$d/uptime.txt" 2>&1
df -h "$HOME" > "$d/disk.txt" 2>&1
if [ -r /proc/meminfo ]; then grep -E "^(MemTotal|MemFree)" /proc/meminfo > "$d/mem.txt"; else echo "DENIED on this target" > "$d/mem.txt"; fi
t="$base/snapshot.tgz"; tar -czf "$t" -C "$(dirname "$d")" "$(basename "$d")" >/dev/null 2>&1 && echo "$t"')

MSYS_NO_PATHCONV=1 gw sftp get "$SNAP" ./
```

```
snap-20260101-120000/_README.txt
snap-20260101-120000/disk.txt
snap-20260101-120000/mem.txt
snap-20260101-120000/uptime.txt
```

`_README.txt` is the whole trick: one block turns a pile of text files into
something you can hand to a colleague. Writing no progress chatter to stdout is
what makes `SNAP=…` come back as a clean single value.

Note `$HOME` rather than `/tmp`: on the target measured here `/tmp` existed and
was **read-only**, which turns a working recipe into four "No such file or
directory" lines. Where `/tmp` is not writable, `${TMPDIR:-/tmp}` is the
portable choice.

### Periodic sampling as a job you can walk away from

`gw async` prints a bare job id, so it captures straight into a variable:

```bash
HANDLE=$(gw async 'for i in 1 2 3 4 5; do
  if [ -r /proc/loadavg ]; then L=$(cat /proc/loadavg); else L=$(uptime | sed "s/.*average: *//"); fi
  echo "$(date +%H:%M:%S) load=$L"
  if [ "$i" -lt 5 ]; then sleep 10; fi
done')
gw job "$HANDLE"                  # status, and the real remote PID
gw output "$HANDLE" --follow
```

**Make the loop's last statement a real command.** The obvious way to write that
sleep — `[ "$i" -lt 5 ] && sleep 10` as the final line — is wrong, and wrong in
the worst way: every sample is collected, the output is flawless, and the job
still reports `exit_code: 1`. On the last iteration the test is false, `&&`
short-circuits, and that failed test *becomes* the loop's exit status. `if … fi`
returns `0` when the condition is false, which is what was meant. Measured side
by side: `if … fi` → `done`, `exit_code 0`; `&&` → `failed`, `exit_code 1`.

Read the outcome from the JSON as well, because `gw output` prints the text and
exits `0` whether the job succeeded or failed. `job.exit_code` in
`gw output --json "$HANDLE"` is the only place that truth lives.

### A long task, and where its output lives

Job handles are in memory. Restart the gateway and the remote process keeps
running while becoming unaddressable — so have the far side record its own
whereabouts:

```bash
gw run 'echo "pid=$$ started $(date)" >> "$HOME/longtask.log"
        nohup sh -c "sleep 3600; echo done \$(date) >> \"\$HOME/longtask.log\"" &
        echo "spawned pid=$!"'
```

The log, the PID and the progress are then readable with another call, with no
dependence on any in-memory state:

```bash
gw run 'cat "$HOME/longtask.log"'
```

### The exit code is the truth, not stdout

```bash
gw run 'ls /definitely/not/here'    # → 2, message on stderr
gw run 'echo fine'                  # → 0
```

Two ways scripts lose it. The first is the pipe: `gw run '…' | head; echo $?`
reports `head`'s status, so a failure reads as success.

```bash
OUT=$(gw run 'ls /definitely/not/here' 2>&1); RC=$?
```

The second is `--json`, which exits `0` unconditionally (see above). Pick one
idiom and stay in it: no `--json` and trust the exit code, or `--json` and read
`exit_code` out of the payload.

### Rehearse before you deploy

A destructive step deserves a dry run with the same shape and none of the
consequences:

```bash
gw run 'set -e
        d="${TMPDIR:-/tmp}/rehearsal.$$"; mkdir -p "$d/bin"; cd "$d"
        echo "target dir : $d"
        for f in a b c; do echo x > "bin/$f"; done
        echo "wrote      : $(ls bin | wc -l) files"
        rm -rf "$d"
        echo REHEARSAL_OK'
```

### What a refusal looks like

```console
$ gw run 'reboot'
gw: HTTP 403 FORBIDDEN: matches danger pattern: \breboot\b
$ echo $?
1
```

Exit `1`, message on stderr, nothing sent over SSH. Now the honest part: that
check is a **text filter**, and a text filter cannot tell intent from a string.
`echo reboot` is refused just as hard, and `echo "reb""oot"` passes through and
prints `reboot`. Read a refusal as "you have almost certainly made a mistake",
never as "this cannot happen" — the security section says the same thing from
the other direction.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/run` | `{cmd, cwd?, timeout?}` → stdout/stderr/exit_code/duration_ms |
| `POST` | `/run/stream` | same body, responds with SSE `stdout` / `stderr` / `exit` events |
| `POST` | `/run/async` | `{cmd, cwd?}` → `202` + job handle |
| `GET` | `/jobs` | every job this process knows about |
| `GET` | `/job/{id}` | one job's status |
| `GET` | `/job/{id}/output?tail=&stream=&follow=` | text, or an SSE follow |
| `POST` | `/job/{id}/kill` | `SIGTERM`, then `SIGKILL` |
| `POST` | `/sftp/list` | `{remote}` → name/type/size/mtime |
| `POST` | `/sftp/upload` `/sftp/download` | `{local, remote, recursive}` |
| `POST` | `/sftp/sync` | `{direction: push\|pull, source, target, delete?, dry_run?}` |
| `GET` | `/audit?limit=&action=` | recent audit entries |
| `GET` | `/health` | transport, policy, channel and job state |
| `POST` | `/close` | drop the SSH transport (keepalive reconnects it) |
| `GET` | `/openapi.json` `/routes` | machine-readable contract |
| `GET` | `/` | HTML landing page with copy-pasteable `curl` recipes |

Status codes mean what a client can act on: `400` bad request, `401` missing or
wrong token, `403` policy denied, `404` unknown route or remote path, `502` the
target refused, `503` with `Retry-After` when every SSH channel is busy.

## Command execution works without bash

The gateway wraps detached commands in `exec <shell> -c 'echo $$ ; <cmd>'` so it
learns the real PID of the process it started, and the shell is configurable
(`SSHGW_REMOTE_SHELL`, default `sh`) because `sh` on busybox handles `for`, `if`
and functions perfectly well while `bash` may not exist at all. A command never
gets a `Tty: operation not supported` surprise: no pty is requested.

## Sync semantics

`push` is local → target, `pull` is target → local; `source` and `target` follow
whichever direction you named. A file is copied when its size **or** mtime
differs, so mtimes are restored on both sides after a transfer. If the SSH server
refuses the `utime` call that sets them, the response says so in `warnings` and
the next run falls back to copying — silently re-uploading everything would be
worse than telling you once.

`delete: true` mirrors removals, per directory rather than only at the top level.
`dry_run: true` reports `copied`/`deleted` counts and writes nothing on either
side.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `SSHGW_TARGET` | — | `[user@]host[:port]`; or set `SSHGW_REMOTE_HOST/USER/PORT` |
| `SSHGW_REMOTE_KEY` / `SSHGW_REMOTE_PASS` | — | exactly one is required |
| `SSHGW_KNOWN_HOSTS` | unset | if set, unknown host keys are **rejected** |
| `SSHGW_LISTEN_HOST` / `SSHGW_LISTEN_PORT` | `127.0.0.1` / `8023` | HTTP bound to loopback by default; any other address requires `SSHGW_TOKEN` |
| `SSHGW_TOKEN` | unset | require `Authorization: Bearer <token>` |
| `SSHGW_TLS_CERT` / `SSHGW_TLS_KEY` | unset | serve HTTPS |
| `SSHGW_POLICY_MODE` | `blacklist` | `off` \| `blacklist` \| `whitelist` |
| `SSHGW_WHITELIST_FILE` | unset | rule file (see below) |
| `SSHGW_DANGEROUS` | unset | extra danger regexes, comma-separated |
| `SSHGW_MAX_CHANNELS` | `8` | concurrent SSH channels; keep ≤ sshd `MaxSessions` |
| `SSHGW_CHANNEL_QUEUE_TIMEOUT` | `30` | seconds to wait for a channel before `503` |
| `SSHGW_REMOTE_SHELL` | `sh` | shell used to launch detached commands |
| `SSHGW_STATE_DIR` | `./.sshgw-state` | audit log and job output files |
| `SSHGW_AUDIT` | `1` | record every operation |
| `SSHGW_JOB_MAX_RUNTIME` | `3600` | cap on a single job's runtime |

`ssh-gateway --print-config` prints the resolved configuration with secrets
redacted.

## Security: read this before exposing it

**The command policy is a guardrail, not a security boundary.** It exists to stop
accidents and the more obvious prompt-injection tricks. In `whitelist` mode it
refuses shell control operators (`;`, `&&`, `|`, `$(`, backticks, newlines),
because a prefix rule like `ls` would otherwise authorize `ls; rm -rf /`. Rules
are matched as *prefixes of the command line*, so write `ls ` or a `rx:^ls(\s|$)`
line to mean "this program only". And a whitelisted program with an escape hatch
(`python3 -`, `vim`, `find -exec`) is still a shell. If you need real isolation,
it has to come from the remote account, not from this file.

- **Bind loopback by default — and that one is enforced.** The gateway refuses
  to start on an address that is not loopback unless `SSHGW_TOKEN` is set,
  because unauthenticated `POST /run` is a remote shell for whoever can open
  the port. Reach it through an SSH tunnel or an authenticating reverse proxy
  rather than widening the bind.
- **Set a token.** `/` and `/run*` are unauthenticated without one. With a token
  set, `/health`, `/openapi.json` and `/routes` stay public so a supervisor can
  probe, and `/health` stops naming the target.
- **Set `SSHGW_KNOWN_HOSTS`** in any environment where a man in the middle is
  conceivable; the default is trust-on-first-use with a warning.
- **The audit log stores commands verbatim.** `.sshgw-state/audit.jsonl` is
  plaintext: treat it as sensitive, and keep it out of version control (the
  `.gitignore` in this repo already does).
- Credentials come from the environment or a key file, never from argv.

## Testing

```bash
pip install -e '.[dev]'
pytest                       # unit tests: no network, no device, ~2 seconds
SSHGW_TEST_DOCKER=1 pytest -m integration
```

The unit suite runs the full HTTP surface against a fake SSH backend, including
SFTP against an in-memory remote filesystem.

The integration suite (`tests/fixtures/sshd/Dockerfile`) starts a real OpenSSH
server on Alpine **with no bash installed**. That is the interesting failure
mode: command chaining, detached jobs and PID reporting all have to work under
plain `sh`, and SFTP has to work against a real `sftp-server` instead of
paramiko's own assumptions. To reuse a container you started yourself:
`SSHGW_TEST_DOCKER=1 SSHGW_TEST_SSHD_CONTAINER=gw-target pytest -m integration`.

`tests/test_hygiene.py` greps the repository for private addresses, personal
paths and key material, so nothing device-specific can be committed by
accident.

## Limitations

- One SSH target per process. Run two gateways for two devices.
- Jobs are tracked in memory: restarting the gateway loses their handles (the
  remote processes keep running, they just become unaddressable).
- SFTP is serialised — one transfer session at a time — because a single
  paramiko transport multiplexes poorly under parallel bulk transfers.
- Interactive programs (anything needing a tty, password prompts, editors) are
  out of scope by design.

## License

MIT. See [LICENSE](LICENSE).
