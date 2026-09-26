"""HTTP layer (server.py)

Responsibilities:
  - route requests, including dynamic segments like ``/job/{id}/kill``
  - enforce bearer auth, with ``/``, ``/health``, ``/openapi.json`` and
    ``/routes`` public so status and docs stay reachable without a token
  - render the envelope and map typed errors onto HTTP status codes, so a
    client can back off on 503 and read 502 as "the device refused"
  - serve SSE for streamed execution and for following a job's output

Note on the request object: handlers receive it as an argument. An earlier
version reached for "whichever handler is currently working" through a
module-level object, which under a threaded server can hand one client's
response to another client's socket. Nothing in this module may be per-process
mutable state.
"""

from __future__ import annotations

import json
import logging
import ssl
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from .models import (
    ERR_BAD_REQUEST,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERR_UNAUTHORIZED,
    BadRequest,
    GatewayError,
    envelope,
)
from .sse import sse_event, sse_headers

log = logging.getLogger("ssh_gateway.server")

#: reachable without a token
PUBLIC_PATHS = {"/", "/health", "/openapi.json", "/routes"}
#: once a token is set, the landing page stops being public: it names the device
PUBLIC_PATHS_WITH_AUTH = PUBLIC_PATHS - {"/"}

Handler = Callable[[dict, Optional[dict], "GatewayHandler"], None]


class _Route:
    __slots__ = ("method", "segs", "handler", "name")

    def __init__(self, method: str, segs: list[str], handler: Handler, name: str):
        self.method, self.segs, self.handler, self.name = method, segs, handler, name


class Router:
    """Tiny route table supporting ``/job/{id}/kill`` params; first match wins."""

    def __init__(self):
        self._routes: list[_Route] = []

    def register(self, method: str, path: str, handler: Handler, name: str = "") -> None:
        segs = [s for s in path.split("/") if s]
        self._routes.append(_Route(method.upper(), segs, handler, name or path))

    def match(self, method: str, path: str) -> Optional[tuple[_Route, dict]]:
        method = method.upper()
        got = [s for s in path.split("/") if s]
        for route in self._routes:
            if route.method != method or len(route.segs) != len(got):
                continue
            params: dict = {}
            for pat, value in zip(route.segs, got):
                if pat.startswith("{") and pat.endswith("}"):
                    params[pat[1:-1]] = value
                elif pat != value:
                    break
            else:
                return route, params
        return None

    def describe(self) -> dict:
        grouped: dict[str, list[str]] = {}
        for route in self._routes:
            path = "/" + "/".join("{" + s[1:-1] + "}" if s.startswith("{") else s
                                  for s in route.segs)
            grouped.setdefault(path, []).append(route.method)
        return {"routes": [{"path": p, "methods": m} for p, m in grouped.items()]}


class GatewayHandler(BaseHTTPRequestHandler):
    """One request; all per-request state lives on this instance."""

    protocol_version = "HTTP/1.1"

    # injected once by make_server
    center = None
    router: Optional[Router] = None

    _headers_sent = False
    meta: dict = {}

    def setup(self):
        super().setup()
        self._headers_sent = False
        # self.path only exists once parse_request() has run, so the real
        # request metadata is filled in by _dispatch.
        self.meta = {"method": "?", "path": "",
                     "client": self.client_address[0] if self.client_address else "",
                     "query": {}}

    # ------------------------------------------------------------------
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        assert self.router is not None and self.center is not None
        parsed = urlparse(self.path)
        self.meta = {**self.meta, "method": method, "path": parsed.path,
                     "query": parse_qs(parsed.query)}

        matched = self.router.match(method, parsed.path)
        if matched is None:
            self._json(404, envelope(False, None, f"no route for {method} {parsed.path}",
                                     ERR_NOT_FOUND))
            return
        route, params = matched

        public = PUBLIC_PATHS if not self.center.auth.enabled else PUBLIC_PATHS_WITH_AUTH
        if parsed.path not in public:
            ok, reason = self.center.auth.check_header(self.headers.get("Authorization"))
            if not ok:
                self.center.audit.record(action="auth_fail", method=method,
                                         path=parsed.path, client=self.meta["client"],
                                         result="denied", detail=reason)
                self._json(401, envelope(False, None, reason, ERR_UNAUTHORIZED))
                return

        body: Optional[dict] = None
        if method == "POST":
            try:
                body = self._read_json()
            except ValueError as e:
                self._json(400, envelope(False, None, f"invalid JSON body: {e}",
                                         ERR_BAD_REQUEST))
                return

        try:
            route.handler(params, body, self)
        except GatewayError as e:
            self._fail(e)
        except Exception as e:  # noqa: BLE001 - keep the server alive, say 500
            log.exception("%s %s failed", method, parsed.path)
            self._fail(GatewayError(f"{type(e).__name__}: {e}",
                                    code=ERR_INTERNAL, http_status=500))

    def _fail(self, err: GatewayError) -> None:
        """Render an error, unless the response is already on the wire."""
        if self._headers_sent:
            log.debug("error after headers were sent: %s", err)
            return
        headers = {}
        retry_after = getattr(err, "retry_after", None)
        if retry_after:
            headers["Retry-After"] = str(retry_after)
        self._json(err.http_status, envelope(False, None, str(err), err.code), headers)

    # ------------------------------------------------------------------
    def send_response(self, code, message=None):  # noqa: D102
        self._headers_sent = True
        super().send_response(code, message)

    def _json(self, status: int, obj: dict, extra_headers: Optional[dict] = None) -> None:
        self._respond(status, json.dumps(obj, ensure_ascii=False).encode(),
                      "application/json; charset=utf-8", extra_headers)

    def _raw(self, status: int, payload: bytes, content_type: str) -> None:
        self._respond(status, payload, content_type)

    def _respond(self, status: int, payload: bytes, content_type: str,
                 extra_headers: Optional[dict] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # The caller hung up while we were working (its own timeout, or
            # Ctrl-C in gw). Ordinary, so it must not print a traceback.
            log.debug("client left before %s was delivered", status)

    def begin_sse(self) -> None:
        self.send_response(200)
        for k, v in sse_headers().items():
            self.send_header(k, v)
        self.end_headers()
        self.close_connection = True  # matches the Connection: close header

    def send_sse(self, event: str, data) -> None:
        self.wfile.write(sse_event(event, data).encode())
        self.wfile.flush()

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        obj = json.loads(raw or b"{}")
        if not isinstance(obj, dict):
            raise ValueError("body must be a JSON object")
        return obj

    def log_message(self, *args):  # our own structured logging instead
        pass


# ======================================================================
# Route table
# ======================================================================
def build_router(center) -> Router:
    router = Router()

    def index(_p, _b, h):
        h._raw(200, center.build_index_html(router), "text/html; charset=utf-8")
    router.register("GET", "/", index, "index")

    def health(_p, _b, h):
        h._json(200, envelope(True, center.health()))
    router.register("GET", "/health", health, "health")

    def routes(_p, _b, h):
        h._json(200, envelope(True, router.describe()))
    router.register("GET", "/routes", routes, "routes")

    def openapi(_p, _b, h):
        h._json(200, center.openapi_spec())
    router.register("GET", "/openapi.json", openapi, "openapi")

    # ---- execution ----
    def run(_p, b, h):
        data = center.run_sync(_cmd(b), _str(b.get("cwd")),
                               timeout=_num(b, "timeout", 30), meta=h.meta)
        h._json(200, envelope(True, data))
    router.register("POST", "/run", run, "run")

    def run_stream(_p, b, h):
        _stream_command(h, center, _cmd(b), _str(b.get("cwd")))
    router.register("POST", "/run/stream", run_stream, "run_stream")

    def run_async(_p, b, h):
        # 202 Accepted: the reply is a handle, the work is still in progress.
        h._json(202, envelope(True, center.submit_job(_cmd(b), _str(b.get("cwd")),
                                                      meta=h.meta)))
    router.register("POST", "/run/async", run_async, "run_async")

    # ---- jobs ----
    def jobs(_p, _b, h):
        h._json(200, envelope(True, {"jobs": center.jobs.list()}))
    router.register("GET", "/jobs", jobs, "jobs")

    def job_status(p, _b, h):
        h._json(200, envelope(True, center.job_status(p["id"])))
    router.register("GET", "/job/{id}", job_status, "job_status")

    def job_output(p, _b, h):
        q = h.meta["query"]
        stream = q.get("stream", ["stdout"])[0]
        if q.get("follow", ["0"])[0] in ("1", "true", "yes"):
            _stream_job_output(h, center, p["id"], stream)
            return
        tail = int(q.get("tail", ["0"])[0] or 0)
        h._json(200, envelope(True, center.job_output(p["id"], tail=tail, stream=stream)))
    router.register("GET", "/job/{id}/output", job_output, "job_output")

    def job_kill(p, _b, h):
        result = center.job_kill(p["id"])
        h._json(200, envelope(True, result))
    router.register("POST", "/job/{id}/kill", job_kill, "job_kill")

    # ---- sftp ----
    def sftp_list(_p, b, h):
        remote = _str(b.get("remote"))
        if not remote:
            raise BadRequest("remote is required", code=ERR_BAD_REQUEST)
        h._json(200, envelope(True, center.sftp_list(remote)))
    router.register("POST", "/sftp/list", sftp_list, "sftp_list")

    def sftp_upload(_p, b, h):
        stats = center.sftp_upload(_str(b.get("local")), _str(b.get("remote")),
                                   _bool(b.get("recursive")))
        h._json(200, envelope(True, stats))
    router.register("POST", "/sftp/upload", sftp_upload, "sftp_upload")

    def sftp_download(_p, b, h):
        stats = center.sftp_download(_str(b.get("remote")), _str(b.get("local")),
                                     _bool(b.get("recursive")))
        h._json(200, envelope(True, stats))
    router.register("POST", "/sftp/download", sftp_download, "sftp_download")

    def sftp_sync(_p, b, h):
        stats = center.sftp_sync(_str(b.get("direction")) or "push",
                                 _str(b.get("source")), _str(b.get("target")),
                                 _bool(b.get("delete")), _bool(b.get("dry_run")))
        h._json(200, envelope(True, stats))
    router.register("POST", "/sftp/sync", sftp_sync, "sftp_sync")

    # ---- operations ----
    def audit(_p, _b, h):
        q = h.meta["query"]
        limit = int(q.get("limit", ["100"])[0] or 100)
        action = q.get("action", [""])[0] or None
        h._json(200, envelope(True, {"entries": center.audit.recent(limit, action)}))
    router.register("GET", "/audit", audit, "audit")

    def close(_p, _b, h):
        center.backend.close()
        h._json(200, envelope(True, {"closed": True}))
    router.register("POST", "/close", close, "close")

    return router


# ======================================================================
# SSE
# ======================================================================
def _stream_command(h: GatewayHandler, center, cmd: str, cwd: str) -> None:
    """POST /run/stream -> ``started``, then stdout/stderr chunks, then exit."""
    h.begin_sse()
    _emit(h, "started", {"cmd": cmd})

    def on_event(kind: str, text: str) -> None:
        _emit(h, kind, {"text": text})

    try:
        result = center.run_stream(cmd, cwd, on_event, meta=h.meta)
        _emit(h, "exit", {"exit_code": result["exit_code"],
                          "duration_ms": result["duration_ms"]})
    except (BrokenPipeError, ConnectionResetError):
        log.debug("client left mid-stream")
    except GatewayError as e:
        _emit(h, "error", {"error": str(e), "code": e.code})
    except Exception as e:  # noqa: BLE001
        log.exception("stream failed")
        _emit(h, "error", {"error": f"{type(e).__name__}: {e}", "code": ERR_INTERNAL})


def _stream_job_output(h: GatewayHandler, center, job_id: str, stream: str) -> None:
    """GET /job/{id}/output?follow=1 -> tail the job file until the job ends."""
    h.begin_sse()
    offset = 0
    try:
        while True:
            snapshot = center.job_output(job_id, tail=0, stream=stream)
            text, info = snapshot["tail"], snapshot["job"]
            if len(text) > offset:
                _emit(h, "stdout", {"text": text[offset:]})
                offset = len(text)
            if info["status"] in ("done", "failed", "killed"):
                _emit(h, "exit", {"status": info["status"],
                                  "exit_code": info["exit_code"]})
                return
            time.sleep(0.3)
    except (BrokenPipeError, ConnectionResetError):
        log.debug("client left mid-follow")
    except GatewayError as e:
        _emit(h, "error", {"error": str(e), "code": e.code})


def _emit(h: GatewayHandler, event: str, data) -> None:
    """Write one SSE frame, swallowing a client that has already hung up."""
    try:
        h.send_sse(event, data)
    except (BrokenPipeError, ConnectionResetError, OSError):
        log.debug("SSE write failed for %s", event)


# ======================================================================
# Body helpers
# ======================================================================
def _str(value) -> str:
    return "" if value is None else str(value)


def _bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _num(b: dict, key: str, default: float) -> float:
    try:
        return float(_str(b.get(key)) or default)
    except ValueError:
        return float(default)


def _cmd(b: Optional[dict]) -> str:
    cmd = _str((b or {}).get("cmd"))
    if not cmd.strip():
        raise BadRequest("cmd is required", code=ERR_BAD_REQUEST)
    return cmd


# ======================================================================
# Server assembly
# ======================================================================
def make_server(center, config, router) -> ThreadingHTTPServer:
    GatewayHandler.center = center
    GatewayHandler.router = router

    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    server = _Server((config.listen_host, config.listen_port), GatewayHandler)
    if config.tls_enabled:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=config.tls_cert, keyfile=config.tls_key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        log.info("HTTPS enabled on %s:%s", config.listen_host, config.listen_port)
    return server
