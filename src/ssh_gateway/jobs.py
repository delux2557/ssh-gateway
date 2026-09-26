"""Background jobs (jobs.py)

A job is a command that outlives the HTTP request that started it: it runs on
its own SSH channel, streams into local files, and is addressed by an opaque
``job_id``. Killing works through the remote PID the backend echoes back on
spawn.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional

from .config import Config
from .models import Job, now_iso

log = logging.getLogger("ssh_gateway.jobs")

_PID_GRACE_S = 10


class JobManager:
    """Thread-safe registry of background jobs with on-disk output."""

    def __init__(self, config: Config, backend, audit=None):
        self.cfg = config
        self.backend = backend
        self.audit = audit
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._job_dir = os.path.join(config.state_dir, "jobs")
        os.makedirs(self._job_dir, exist_ok=True)

    # ------------------------------------------------------------------
    def submit(self, cmd: str, cwd: str = "") -> Job:
        job = Job(job_id=uuid.uuid4().hex[:12], cmd=cmd, cwd=cwd or None)
        job.output_files = {
            "stdout": os.path.join(self._job_dir, job.job_id + ".out"),
            "stderr": os.path.join(self._job_dir, job.job_id + ".err"),
        }
        with self._lock:
            self._jobs[job.job_id] = job
        self._audit("submit", cmd=cmd, target=cwd or None, job_id=job.job_id)
        threading.Thread(target=self._run_worker, args=(job,),
                         name="job-" + job.job_id, daemon=True).start()
        return job

    def _run_worker(self, job: Job) -> None:
        job.status = "running"
        job.started_at = now_iso()
        out_f = open(job.output_files["stdout"], "w", encoding="utf-8", errors="replace")
        err_f = open(job.output_files["stderr"], "w", encoding="utf-8", errors="replace")

        def on_out(text: str) -> None:
            out_f.write(text)
            out_f.flush()

        def on_err(text: str) -> None:
            err_f.write(text)
            err_f.flush()

        try:
            proc = self.backend.spawn(job.cmd, job.cwd or "", on_stdout=on_out,
                                      on_stderr=on_err)
            # The PID arrives as the first stdout line. Stop waiting as soon as
            # the process ends: a command that dies instantly (missing shell,
            # bad path) must report failure immediately, not after the grace.
            deadline = time.time() + _PID_GRACE_S
            while proc.pid is None and not proc.finished and time.time() < deadline:
                time.sleep(0.05)
            job.pid = proc.pid
            cap = min(self.cfg.job_max_runtime, 3600) if self.cfg.job_max_runtime else None
            job.exit_code = proc.wait(timeout=cap)
            if job.status != "killed":
                job.status = "done" if job.exit_code == 0 else "failed"
        except Exception as e:  # spawn/wait blew up
            job.status = "failed"
            job.exit_code = -1
            with open(job.output_files["stderr"], "a", encoding="utf-8") as f:
                f.write(f"job error: {type(e).__name__}: {e}\n")
            log.warning("job %s failed: %s", job.job_id, e)
        finally:
            out_f.close()
            err_f.close()
            job.finished_at = now_iso()
            self._audit("finish", job_id=job.job_id, status=job.status,
                        exit=job.exit_code, pid=job.pid)

    # ------------------------------------------------------------------
    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.dict() for j in jobs]

    def status(self, job_id: str) -> Job:
        return self._require(job_id)

    def output(self, job_id: str, tail: int = 0, stream: str = "stdout") -> tuple[str, dict]:
        job = self._require(job_id)
        path = job.output_files.get(stream) or job.output_files["stdout"]
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                data = f.read()
        except FileNotFoundError:
            data = ""
        return (data[-tail:] if tail > 0 else data), job.dict()

    def kill(self, job_id: str) -> dict:
        job = self._require(job_id)
        if job.status in ("done", "failed", "killed"):
            return {"job_id": job_id, "killed": False, "reason": "already finished"}
        if not job.pid:
            # No PID means the remote process never reported one, so there is
            # nothing to signal — say so rather than pretending it worked.
            return {"job_id": job_id, "killed": False,
                    "reason": "remote PID not yet known; retry in a moment"}
        try:
            self.backend.kill_pid(job.pid)
        except Exception as e:
            return {"job_id": job_id, "killed": False, "reason": str(e)}
        job.status = "killed"
        self._audit("kill", job_id=job_id, pid=job.pid)
        return {"job_id": job_id, "killed": True, "pid": job.pid}

    def _require(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"unknown job id: {job_id}")
        return job

    def _audit(self, action: str, **kw) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(action="job_" + action, cmd=kw.get("cmd"),
                              target=kw.get("target"), detail=kw)
        except Exception:
            pass
