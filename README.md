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
| `SSHGW_LISTEN_HOST` / `SSHGW_LISTEN_PORT` | `127.0.0.1` / `8023` | HTTP bound to loopback by default |
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

- **Bind loopback by default.** Reach the gateway through an SSH tunnel or a
  reverse proxy with auth, not by setting `SSHGW_LISTEN_HOST=0.0.0.0`.
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
