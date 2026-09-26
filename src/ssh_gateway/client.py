"""``gw`` — a thin CLI over the gateway's HTTP API.

Standard library only, so it can be dropped on a machine and run. The point of
sending the command as a *JSON string value* rather than through a shell is
that it never round-trips locally: ``$VAR``, quotes and pipes arrive at the
remote shell intact regardless of whether you are in bash, zsh or PowerShell.

    gw run 'echo $HOME'              # remote expansion, local untouched
    gw stream 'dmesg -w'             # SSE, prints as it arrives
    gw async 'make -j4'              # returns a job id
    gw output <id> --follow
    gw sftp put ./src /tmp/src -r
    gw status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Optional

DEFAULT_URL = os.environ.get("GW_URL", "http://127.0.0.1:8023")
TOKEN = os.environ.get("GW_TOKEN", "")


class GatewayCall(Exception):
    """A request the gateway answered with ok=false, or did not answer."""


def _request(url: str, path: str, body: Optional[dict] = None,
             token: str = TOKEN, timeout: float = 600.0, method: Optional[str] = None):
    """Call one endpoint. Returns (http_status, envelope_or_bytes)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data,
                                 method=method or ("POST" if data else "GET"))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _decode(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, _decode(e.read())
    except urllib.error.URLError as e:
        raise GatewayCall(f"cannot reach {url}: {e.reason}") from e


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except (ValueError, AttributeError):
        return raw


def _unwrap(status: int, parsed: Any) -> Any:
    if not isinstance(parsed, dict) or "ok" not in parsed:
        return parsed
    if not parsed.get("ok"):
        raise GatewayCall(f"HTTP {status} {parsed.get('code') or ''}: "
                          f"{parsed.get('error') or 'unknown error'}".strip())
    return parsed["data"]


def _show(data: Any, as_json: bool) -> int:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return 0
    if isinstance(data, dict):
        for key in ("stdout", "stderr"):
            if key in data and data[key]:
                sys.stdout.write(str(data[key]))
        if "stdout" not in data and "stderr" not in data:
            for k, v in data.items():
                print(f"{k}: {v}")
        if "exit_code" in data and data["exit_code"]:
            return int(data["exit_code"])
    elif data is not None:
        print(data)
    return 0


def _stream(url: str, path: str, body: Optional[dict], token: str) -> int:
    """Read an SSE endpoint frame by frame and echo it."""
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=None if body is None else json.dumps(body).encode(),
                                 method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    code = 0
    try:
        with urllib.request.urlopen(req, timeout=None) as resp:
            event = ""
            for line in resp:
                text = line.decode(errors="replace").rstrip("\r\n")
                if text.startswith("event:"):
                    event = text.split(":", 1)[1].strip()
                elif text.startswith("data:"):
                    payload = text.split(":", 1)[1].strip()
                    if event in ("stdout", "stderr"):
                        sys.stdout.write(json.loads(payload)["text"])
                        sys.stdout.flush()
                    elif event == "exit":
                        code = int(json.loads(payload).get("exit_code") or 0)
                    elif event == "error":
                        raise GatewayCall(json.loads(payload).get("error", "stream error"))
    except urllib.error.URLError as e:
        raise GatewayCall(f"cannot reach {url}: {e.reason}") from e
    return code


# ======================================================================
# verbs
# ======================================================================
def cmd_run(a) -> int:
    status, parsed = _request(a.url, "/run", {"cmd": a.cmd, "cwd": a.cwd,
                                              "timeout": a.timeout}, a.token)
    return _show(_unwrap(status, parsed), a.json)


def cmd_stream(a) -> int:
    return _stream(a.url, "/run/stream", {"cmd": a.cmd, "cwd": a.cwd}, a.token)


def cmd_async(a) -> int:
    status, parsed = _request(a.url, "/run/async", {"cmd": a.cmd, "cwd": a.cwd}, a.token)
    job = _unwrap(status, parsed)
    print(job.get("job_id") if not a.json else json.dumps(job, indent=2))
    return 0


def cmd_jobs(a) -> int:
    status, parsed = _request(a.url, "/jobs", None, a.token)
    data = _unwrap(status, parsed)
    if a.json:
        return _show(data, True)
    for j in data.get("jobs", []):
        print(f"{j['job_id']}  {j['status']:<8} exit={j['exit_code']}  {j['cmd'][:60]}")
    return 0


def cmd_job(a) -> int:
    status, parsed = _request(a.url, f"/job/{a.id}", None, a.token)
    return _show(_unwrap(status, parsed), a.json)


def cmd_output(a) -> int:
    if a.follow:
        return _stream(a.url, f"/job/{a.id}/output?follow=1&stream={a.stream}", None, a.token)
    status, parsed = _request(
        a.url, f"/job/{a.id}/output?stream={a.stream}"
               + (f"&tail={a.tail}" if a.tail else ""), None, a.token)
    data = _unwrap(status, parsed)
    if a.json:
        return _show(data, True)
    sys.stdout.write(data.get("tail", ""))
    return 0


def cmd_kill(a) -> int:
    status, parsed = _request(a.url, f"/job/{a.id}/kill", {}, a.token)
    return _show(_unwrap(status, parsed), a.json)


def cmd_sftp(a) -> int:
    verb, rest = a.verb, a.rest
    if verb == "ls":
        body = {"remote": rest[0]}
        status, parsed = _request(a.url, "/sftp/list", body, a.token)
        data = _unwrap(status, parsed)
        if a.json:
            return _show(data, True)
        for item in data.get("items", []):
            flag = "/" if item["type"] == "dir" else ""
            print(f"{item['size']:>10}  {item['name']}{flag}")
        return 0
    if verb in ("put", "get", "sync"):
        recursive = bool(getattr(a, "recursive", False))
        if verb == "sync":
            body = {"direction": a.direction, "source": rest[0], "target": rest[1],
                    "delete": bool(a.delete), "dry_run": bool(a.dry_run)}
            path = "/sftp/sync"
        else:
            local, remote = (rest[0], rest[1]) if verb == "put" else (rest[1], rest[0])
            body = {"local": os.path.abspath(local), "remote": remote,
                    "recursive": recursive}
            path = "/sftp/upload" if verb == "put" else "/sftp/download"
        status, parsed = _request(a.url, path, body, a.token, timeout=3600)
        return _show(_unwrap(status, parsed), a.json)
    raise GatewayCall(f"unknown sftp verb: {verb} (ls|put|get|sync)")


def cmd_status(a) -> int:
    status, parsed = _request(a.url, "/health", None, a.token)
    return _show(_unwrap(status, parsed), True if a.json else False)


def cmd_routes(a) -> int:
    status, parsed = _request(a.url, "/openapi.json", None, a.token)
    spec = _unwrap(status, parsed)
    if a.json:
        return _show(spec, True)
    print(f"{spec['info']['title']} v{spec['info']['version']}  @ {spec['servers'][0]['url']}")
    for path, methods in spec["paths"].items():
        for verb, meta in methods.items():
            schema = (meta.get("requestBody") or {}).get("content", {})
            props = next(iter(schema.values()), {}).get("schema", {}).get("properties", {})
            args = " ".join(f"{k}:{v['type']}" for k, v in props.items())
            print(f"  {verb.upper():<5} {path:<20} {meta.get('summary', '')}"
                  + (f"   [{args}]" if args else ""))
    return 0


# ======================================================================
# parser
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="gw",
        description="Call an SSH Gateway from the command line.",
        epilog="The command is sent as a JSON string, so it never passes through "
               "your local shell: $VAR, quotes and pipes arrive remotely as typed.")
    ap.add_argument("--url", default=DEFAULT_URL,
                    help=f"gateway base URL (default {DEFAULT_URL}, env GW_URL)")
    ap.add_argument("--token", default=TOKEN, help="bearer token (env GW_TOKEN)")
    ap.add_argument("--json", action="store_true", help="print the raw response object")
    sub = ap.add_subparsers(dest="sub", required=True)

    for name, fn, help_text in (("run", cmd_run, "run a command and wait for it"),
                                ("stream", cmd_stream, "run a command, print output live"),
                                ("async", cmd_async, "run a command detached, print job id")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("cmd", help="command for the remote shell")
        sp.add_argument("--cwd", default="", help="remote working directory")
        if name == "run":
            sp.add_argument("--timeout", type=float, default=30, help="seconds")
        sp.set_defaults(func=fn)

    sp = sub.add_parser("jobs", help="list jobs")
    sp.set_defaults(func=cmd_jobs)

    sp = sub.add_parser("job", help="one job's status")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_job)

    sp = sub.add_parser("output", help="read a job's output")
    sp.add_argument("id")
    sp.add_argument("--follow", action="store_true", help="stream until the job ends")
    sp.add_argument("--tail", type=int, default=0, help="only the last N characters")
    sp.add_argument("--stream", choices=("stdout", "stderr"), default="stdout")
    sp.set_defaults(func=cmd_output)

    sp = sub.add_parser("kill", help="kill a job (TERM, then KILL)")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_kill)

    sp = sub.add_parser("sftp", help="ls | put | get | sync against the device")
    sp.add_argument("verb", choices=("ls", "put", "get", "sync"))
    sp.add_argument("rest", nargs="+",
                    help="ls REMOTE | put LOCAL REMOTE | get REMOTE LOCAL | sync SRC DST")
    sp.add_argument("-r", "--recursive", action="store_true")
    sp.add_argument("--direction", choices=("push", "pull"), default="push",
                    help="sync: push is local->device, pull is device->local")
    sp.add_argument("--delete", action="store_true",
                    help="sync: also remove entries missing on the source side")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_sftp)

    sp = sub.add_parser("status", help="gateway, transport and policy status")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("routes", help="print the endpoint contract the gateway serves")
    sp.set_defaults(func=cmd_routes)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except GatewayCall as e:
        print(f"gw: {e}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, BrokenPipeError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
