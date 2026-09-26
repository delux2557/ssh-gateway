"""Response envelope, DTOs and typed errors (models.py)

Every JSON reply is ``{"ok": bool, "data": ..., "error": str, "code": str}`` so
a machine client can branch on one shape. Errors are raised as typed
exceptions carrying their own HTTP status and a stable string code, which keeps
status-code policy in one place instead of scattered across route handlers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Optional

# ---------- stable error codes ----------
ERR_BAD_REQUEST = "BAD_REQUEST"
ERR_UNAUTHORIZED = "UNAUTHORIZED"
ERR_FORBIDDEN = "FORBIDDEN"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_REMOTE = "REMOTE_ERROR"
ERR_UNAVAILABLE = "UNAVAILABLE"
ERR_TIMEOUT = "TIMEOUT"
ERR_INTERNAL = "INTERNAL_ERROR"


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def envelope(ok: bool, data: Any = None, error: str = "", code: str = "") -> dict:
    return {"ok": ok, "data": data, "error": error, "code": code}


class GatewayError(Exception):
    """Base class for errors the HTTP layer can render directly."""

    code: str = ERR_INTERNAL
    http_status: int = 500

    def __init__(self, message: str = "", *, code: Optional[str] = None,
                 http_status: Optional[int] = None):
        super().__init__(message or self.__class__.__name__)
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class BadRequest(GatewayError):
    code = ERR_BAD_REQUEST
    http_status = 400


class Unauthorized(GatewayError):
    code = ERR_UNAUTHORIZED
    http_status = 401


class Forbidden(GatewayError):
    code = ERR_FORBIDDEN
    http_status = 403


class NotFound(GatewayError):
    code = ERR_NOT_FOUND
    http_status = 404


class RemoteError(GatewayError):
    """The target refused or failed the operation."""
    code = ERR_REMOTE
    http_status = 502


class Unavailable(GatewayError):
    """Transient saturation — the client should back off and retry."""
    code = ERR_UNAVAILABLE
    http_status = 503

    def __init__(self, message: str = "", retry_after: int = 5, **kw):
        super().__init__(message, **kw)
        self.retry_after = retry_after


@dataclass
class ExecResult:
    """Outcome of one command execution."""
    cmd: str
    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int = 0

    def dict(self) -> dict:
        return asdict(self)


@dataclass
class Job:
    """A detached remote process addressed by job_id."""
    job_id: str
    cmd: str
    status: str = "queued"          # queued | running | done | failed | killed
    pid: Optional[int] = None
    cwd: Optional[str] = None
    created_at: str = field(default_factory=now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    exit_code: Optional[int] = None
    output_files: dict = field(default_factory=lambda: {"stdout": "", "stderr": ""})

    def dict(self) -> dict:
        d = asdict(self)
        d["output_files"] = {k: bool(v) for k, v in d["output_files"].items()}
        return d


@dataclass
class AuditEntry:
    """One audited operation."""
    ts: str
    method: str
    path: str
    client: str
    action: str                 # run / stream / job_submit / sftp_upload / auth_fail ...
    cmd: Optional[str] = None
    target: Optional[str] = None
    result: str = "ok"          # ok | denied | error
    detail: Any = None

    def dict(self) -> dict:
        return asdict(self)
