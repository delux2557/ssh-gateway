"""Facade and composition root (center.py)

GatewayCenter wires backend / sftp / jobs / policy / audit / auth together and
exposes one cohesive operation set. Route handlers depend on this facade only
and never touch paramiko. Every operation returns plain data or raises a typed
:class:`~ssh_gateway.models.GatewayError`; turning those into HTTP status codes
is the server's job, so the domain layer stays transport-agnostic.
"""

from __future__ import annotations

import html as _html
import logging
import threading
import time
from typing import Callable, Optional

from .audit import AuditLogger
from .auth import TokenAuth
from .backend import ChannelBusy, SSHBackend
from .config import Config
from .jobs import JobManager
from .models import (
    ERR_BAD_REQUEST,
    ERR_NOT_FOUND,
    BadRequest,
    Forbidden,
    NotFound,
    RemoteError,
    Unavailable,
)
from .policy import CommandPolicy
from .sftp import SFTPService

log = logging.getLogger("ssh_gateway.center")


def html_escape(s: str) -> str:
    return _html.escape(s or "")


class GatewayCenter:
    def __init__(self, config: Optional[Config] = None, backend=None):
        self.cfg = config or Config()
        if backend is None:
            self.cfg.validate()  # an injected fake needs no real target
        self.cfg.ensure_dirs()
        self._boot = time.time()

        self.backend = backend or SSHBackend(self.cfg)
        self.audit = AuditLogger(enabled=self.cfg.audit_enabled,
                                 file=self.cfg.audit_file,
                                 max_entries=self.cfg.audit_max_entries)
        self.policy = CommandPolicy(mode=self.cfg.policy_mode,
                                    whitelist_file=self.cfg.whitelist_file,
                                    dangerous_patterns=self.cfg.dangerous_patterns,
                                    allow_everything_when_no_whitelist=
                                    self.cfg.allow_everything_when_no_whitelist)
        self.auth = TokenAuth(self.cfg.api_token)
        self.sftp = SFTPService(self.backend)
        self.jobs = JobManager(self.cfg, self.backend, audit=self.audit)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Open the port immediately and connect in the background.

        A target that is briefly unreachable (device asleep, link down) must
        not stop the gateway: the keepalive thread picks the transport up.
        """
        self.backend.start_keepalive()

        def _connect_soon():
            try:
                self.backend.ensure_connected()
            except Exception as e:
                log.warning("target unreachable at startup, keepalive will retry: %s", e)

        threading.Thread(target=_connect_soon, name="gw-connect", daemon=True).start()

    def shutdown(self) -> None:
        try:
            self.backend.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------
    def run_sync(self, cmd: str, cwd: str = "", timeout: Optional[float] = None,
                 meta: Optional[dict] = None) -> dict:
        meta = meta or {}
        self._authorize(cmd, "run", meta)
        result = self._remote(self.backend.exec, cmd, cwd, timeout=timeout)
        self._audit(meta, "run", cmd, cwd, {"exit": result.exit_code})
        return result.dict()

    def run_stream(self, cmd: str, cwd: str, on_event: Callable,
                   meta: Optional[dict] = None) -> dict:
        meta = meta or {}
        self._authorize(cmd, "stream", meta)
        result = self._remote(self.backend.exec_stream, cmd, cwd, on_event)
        self._audit(meta, "stream", cmd, cwd, {"exit": result.exit_code})
        return result.dict()

    def submit_job(self, cmd: str, cwd: str = "",
                   meta: Optional[dict] = None) -> dict:
        meta = meta or {}
        self._authorize(cmd, "job_submit", meta)
        job = self.jobs.submit(cmd, cwd)
        self._audit(meta, "job_submit", cmd, cwd, {"job_id": job.job_id})
        return job.dict()

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def job_status(self, job_id: str) -> dict:
        return self._call(self.jobs.status, job_id).dict()

    def job_output(self, job_id: str, tail: int = 0, stream: str = "stdout") -> dict:
        text, info = self._call(self.jobs.output, job_id, tail=tail, stream=stream)
        return {"tail": text, "job": info}

    def job_kill(self, job_id: str) -> dict:
        return self._call(self.jobs.kill, job_id)

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------
    def sftp_list(self, remote: str) -> dict:
        return {"remote": remote, "server": self.sftp.server_info,
                "items": self._remote(self.sftp.list_dir, remote)}

    def sftp_upload(self, local: str, remote: str, recursive: bool) -> dict:
        stats = self._remote(self.sftp.upload, local, remote, recursive)
        self.audit.record(action="sftp_upload", cmd=None, target=f"{local} -> {remote}",
                          detail=stats)
        return stats

    def sftp_download(self, remote: str, local: str, recursive: bool) -> dict:
        stats = self._remote(self.sftp.download, remote, local, recursive)
        self.audit.record(action="sftp_download", cmd=None, target=f"{remote} -> {local}",
                          detail=stats)
        return stats

    def sftp_sync(self, direction: str, source: str, target: str,
                  delete: bool, dry_run: bool) -> dict:
        stats = self._remote(self.sftp.sync, direction, source, target, delete, dry_run)
        self.audit.record(action="sftp_sync", cmd=None,
                          target=f"{direction}:{source} -> {target}", detail=stats)
        return stats

    # ------------------------------------------------------------------
    # Status / docs
    # ------------------------------------------------------------------
    def health(self) -> dict:
        data = {
            "ok": True,
            "version": self.cfg.version,
            "connected": self.backend.connected(),
            "listen": {"host": self.cfg.listen_host, "port": self.cfg.listen_port},
            "tls": self.cfg.tls_enabled,
            "audit": self.audit.enabled_flag(),
            "policy": self.policy.describe(),
            "auth": self.auth.describe(),
            "channels": {"max": self.cfg.max_channels},
            "jobs_running": sum(1 for j in self.jobs.list() if j["status"] == "running"),
            "uptime_s": round(time.time() - self._boot),
        }
        # /health stays reachable without a token so a supervisor can probe it,
        # which means it must not name the device once auth is on.
        if not self.auth.enabled:
            data["target"] = {"host": self.cfg.remote_host,
                              "port": self.cfg.remote_port,
                              "user": self.cfg.remote_user}
        return data

    def openapi_spec(self) -> dict:
        """A compact OpenAPI document: the machine-readable contract agents read."""
        def body(**fields):
            return {"content": {"application/json": {"schema": {
                "type": "object",
                "properties": {k: {"type": t} for k, t in fields.items()}}}}}

        scheme = "https" if self.cfg.tls_enabled else "http"
        return {
            "openapi": "3.1.0",
            "info": {"title": "SSH Gateway", "version": self.cfg.version,
                     "description": "Run commands and move files on an SSH-only "
                                    "device over HTTP. Every reply is "
                                    "{ok, data, error, code}."},
            "servers": [{"url": f"{scheme}://{self.cfg.listen_host}:{self.cfg.listen_port}"}],
            "paths": {
                "/run": {"post": {"summary": "Run a command, wait for the result",
                                  "requestBody": body(cmd="string", cwd="string",
                                                      timeout="number")}},
                "/run/stream": {"post": {"summary": "Run a command, stream stdout/stderr/exit as SSE",
                                         "requestBody": body(cmd="string", cwd="string")}},
                "/run/async": {"post": {"summary": "Run a command detached, returns job_id",
                                        "requestBody": body(cmd="string", cwd="string")}},
                "/jobs": {"get": {"summary": "List jobs"}},
                "/job/{id}": {"get": {"summary": "Job status"}},
                "/job/{id}/output": {"get": {"summary": "Job output",
                                             "parameters": [{"name": "stream", "in": "query"},
                                                            {"name": "tail", "in": "query"},
                                                            {"name": "follow", "in": "query"}]}},
                "/job/{id}/kill": {"post": {"summary": "Kill a job (SIGTERM then SIGKILL)"}},
                "/sftp/list": {"post": {"summary": "List a remote directory",
                                        "requestBody": body(remote="string")}},
                "/sftp/upload": {"post": {"summary": "Upload, recursively if requested",
                                          "requestBody": body(local="string", remote="string",
                                                              recursive="boolean")}},
                "/sftp/download": {"post": {"summary": "Download, recursively if requested",
                                            "requestBody": body(remote="string", local="string",
                                                                recursive="boolean")}},
                "/sftp/sync": {"post": {"summary": "Incremental directory sync",
                                        "requestBody": body(direction="string", source="string",
                                                            target="string", delete="boolean",
                                                            dry_run="boolean")}},
                "/audit": {"get": {"summary": "Recent audit entries"}},
                "/health": {"get": {"summary": "Status of gateway, transport and policy"}},
                "/close": {"post": {"summary": "Drop the SSH transport (keepalive reconnects)"}},
                "/openapi.json": {"get": {"summary": "This document"}},
                "/routes": {"get": {"summary": "Route table"}},
            },
        }

    def build_index_html(self, router) -> bytes:
        """Human landing page: the same surface as curl recipes."""
        port = self.cfg.listen_port
        recipes = {
            "run": [f'curl -s -X POST localhost:{port}/run -d \'{{"cmd":"uname -a"}}\''],
            "stream": [f'curl -sN -X POST localhost:{port}/run/stream '
                       f'-d \'{{"cmd":"ping -c 5 localhost"}}\''],
            "async": [f'curl -s -X POST localhost:{port}/run/async '
                      f'-d \'{{"cmd":"sleep 5; echo done"}}\'',
                      f'curl -sN localhost:{port}/job/<id>/output?follow=1',
                      f'curl -s -X POST localhost:{port}/job/<id>/kill'],
            "sftp": [f'curl -s -X POST localhost:{port}/sftp/list '
                     f'-d \'{{"remote":"/var/log"}}\'',
                     f'curl -s -X POST localhost:{port}/sftp/sync '
                     f'-d \'{{"direction":"push","source":"./src","target":"/tmp/synced"}}\''],
            "ops": [f'curl -s localhost:{port}/health', f'curl -s localhost:{port}/audit'],
        }

        def block(group: str) -> str:
            return "\n".join(f'<pre class="code">{html_escape(c)}</pre>'
                             for c in recipes[group])

        rows = "".join(
            f'<tr><td><code>{html_escape(r["path"])}</code></td>'
            f'<td>{html_escape(", ".join(r["methods"]))}</td></tr>'
            for r in router.describe()["routes"])

        doc = f"""<!doctype html><html><head><meta charset="utf-8">
<title>SSH Gateway v{self.cfg.version}</title>
<style>
 body{{font-family:ui-monospace,Menlo,Consolas,monospace;background:#0f172a;color:#e2e8f0;margin:0;padding:24px}}
 h1{{font-size:20px}} h2{{font-size:15px;margin-top:26px;color:#93c5fd}}
 .card{{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px 18px;margin:10px 0}}
 table{{border-collapse:collapse;width:100%;font-size:13px}}
 th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #334155}}
 code{{background:#0b1220;padding:2px 6px;border-radius:4px;font-size:12px}}
 pre.code{{background:#0b1220;padding:10px;border-radius:6px;overflow-x:auto;font-size:12px}}
 .tag{{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;margin-left:8px;background:#065f46;color:#6ee7b7}}
</style></head><body>
<h1>SSH Gateway <span class="tag">v{self.cfg.version}</span></h1>
<p>target <code>{html_escape(self.cfg.remote_user)}@{html_escape(self.cfg.remote_host)}:{self.cfg.remote_port}</code>
 &middot; TLS {html_escape("on" if self.cfg.tls_enabled else "off")}
 &middot; auth {html_escape("bearer token" if self.cfg.api_token else "none")}
 &middot; policy {html_escape(self.cfg.policy_mode)} &middot; port {port}</p>
<div class="card"><h2>Run</h2>{block("run")}</div>
<div class="card"><h2>Stream (SSE)</h2>{block("stream")}</div>
<div class="card"><h2>Detached jobs</h2>{block("async")}</div>
<div class="card"><h2>SFTP</h2>{block("sftp")}</div>
<div class="card"><h2>Operations</h2>{block("ops")}</div>
<h2>Routes</h2>
<table><tr><th>Path</th><th>Methods</th></tr>{rows}</table>
<div class="card" style="margin-top:20px"><b>Envelope:</b>
<code>{{"ok": true, "data": ..., "error": "", "code": ""}}</code>
 &middot; auth header: <code>Authorization: Bearer &lt;token&gt;</code>
 &middot; contract: <code>/openapi.json</code></div>
</body></html>"""
        return doc.encode()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _authorize(self, cmd: str, action: str, meta: dict) -> None:
        """Policy check: denied commands are audited and never reach the device."""
        if not cmd.strip():
            raise BadRequest("cmd must not be empty", code=ERR_BAD_REQUEST)
        allowed, reason = self.policy.check(cmd)
        if not allowed:
            self._audit(meta, action, cmd, "", {"reason": reason}, result="denied")
            raise Forbidden(reason)
        self._audit(meta, action, cmd, "", "allowed")

    def _remote(self, fn, *args, **kw):
        """Run a backend/sftp call, mapping failures onto typed errors."""
        try:
            return fn(*args, **kw)
        except ChannelBusy as e:
            raise Unavailable(str(e)) from e
        except FileNotFoundError as e:
            raise NotFound(str(e), code=ERR_NOT_FOUND) from e
        except ValueError as e:
            raise BadRequest(str(e), code=ERR_BAD_REQUEST) from e
        except Exception as e:
            raise RemoteError(f"{type(e).__name__}: {e}") from e

    def _call(self, fn, *args, **kw):
        """Like _remote but for local registry calls where 404 is the answer."""
        try:
            return fn(*args, **kw)
        except KeyError as e:
            raise NotFound(str(e).strip("'"), code=ERR_NOT_FOUND) from e

    def _audit(self, meta: dict, action: str, cmd: str, cwd: str, detail,
               result: str = "ok") -> None:
        try:
            self.audit.record(action=action, method=meta.get("method", "?"),
                              path=meta.get("path", "?"), client=meta.get("client", ""),
                              cmd=cmd, target=cwd or None, result=result, detail=detail)
        except Exception:
            pass
