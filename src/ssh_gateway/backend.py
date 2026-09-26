"""SSH transport (backend.py)

Hides paramiko behind four operations the HTTP layer needs:
  - exec():         run to completion, return stdout/stderr/exit_code
  - exec_stream():  push output through a callback while the command runs (SSE)
  - spawn():        detached process on its own channel; the first echoed line
                    is the remote PID so the job can be wait()ed and kill()ed
  - sftp():         a live paramiko.SFTPClient

Notes:
  - One persistent transport, one channel per command. A bounded pool keeps the
    gateway below the peer's MaxSessions so channels queue instead of being
    refused by the server.
  - The backend is injected into GatewayCenter, so the whole HTTP surface can
    be exercised against a fake with no device attached.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import threading
import time
from typing import Callable, Optional

import paramiko

from .config import Config
from .models import ExecResult

log = logging.getLogger("ssh_gateway.backend")

StreamCallback = Callable[[str], None]
ExitCallback = Callable[[int], None]


class ChannelBusy(RuntimeError):
    """No channel freed up in time — the client should retry (HTTP 503)."""


class HostKeyNotTrusted(paramiko.SSHException):
    """The target presented a key that SSHGW_KNOWN_HOSTS does not list."""


class _RemoteProcess:
    """Handle for a detached remote process."""

    def __init__(self, backend: SSHBackend, chan, on_stdout: StreamCallback,
                 on_stderr: StreamCallback | None, want_pid: bool,
                 on_finish: Optional[Callable[[], None]] = None):
        self._backend = backend
        self._chan = chan
        self._on_stdout = on_stdout or (lambda _t: None)
        self._on_stderr = on_stderr or (lambda _t: None)
        self._on_finish = on_finish
        self.pid: Optional[int] = None
        self.exit_code: Optional[int] = None
        self._done = threading.Event()
        self._want_pid = want_pid

    @property
    def finished(self) -> bool:
        return self._done.is_set()

    def start(self) -> None:
        threading.Thread(target=self._run, name="proc-pump", daemon=True).start()

    def wait(self, timeout: Optional[float] = None) -> int:
        self._done.wait(timeout=timeout)
        return self.exit_code if self.exit_code is not None else -1

    def kill(self) -> Optional[int]:
        if self.pid is None:
            return None
        try:
            self._backend.kill_pid(self.pid)
        except Exception as e:  # the process may already be gone
            log.debug("kill failed: %s", e)
        return self.pid

    def _run(self) -> None:
        try:
            self._pump()
        finally:
            if self._on_finish:
                self._on_finish()

    def _pump(self) -> None:
        """Drain both streams to EOF, then collect the exit status.

        Each stream gets its own reader thread and is buffered completely; with
        ``want_pid`` only the leading PID line is stripped from stdout, never
        dropped. Reading both streams of one channel from two threads is safe
        under paramiko.
        """

        def reader(stream: str, want_pid: bool) -> None:
            buf = bytearray()
            pid_done = not want_pid
            try:
                while True:
                    d = (self._chan.recv(8192) if stream == "out"
                         else self._chan.recv_stderr(8192))
                    if not d:
                        break
                    buf += d
                    if not pid_done:
                        nl = buf.find(b"\n")
                        if nl == -1:
                            continue
                        raw = bytes(buf[:nl]).decode(errors="replace").strip()
                        if raw.isdigit():
                            self.pid = int(raw)
                        del buf[:nl + 1]
                        pid_done = True
                if buf:
                    text = bytes(buf).decode(errors="replace")
                    (self._on_stdout if stream == "out" else self._on_stderr)(text)
            except Exception as e:
                log.debug("%s reader end: %s", stream, e)

        t1 = threading.Thread(target=reader, args=("out", self._want_pid), daemon=True)
        t2 = threading.Thread(target=reader, args=("err", False), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        try:
            self.exit_code = self._chan.recv_exit_status()
        except Exception as e:
            log.debug("recv_exit_status: %s", e)
            self.exit_code = -1
        finally:
            try:
                self._chan.close()
            except Exception:
                pass
            self._done.set()


class _StrictHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Refuse any host key that is not in SSHGW_KNOWN_HOSTS, and say so usefully.

    paramiko's own RejectPolicy message ("Unknown server host") does not tell an
    operator which file was consulted, which is the first thing they need.
    """

    def __init__(self, path: str):
        self._path = path
        self._keys = paramiko.HostKeys()
        self._keys.load(path)

    def missing_host_key(self, client, hostname: str, key) -> None:
        raise HostKeyNotTrusted(
            f"host key for {hostname} is not in {self._path}; "
            f"add it or unset SSHGW_KNOWN_HOSTS to accept on first use")


class SSHBackend:
    """Owns one persistent SSH transport to the target; serves exec and sftp."""

    def __init__(self, config: Config):
        self.cfg = config
        self._ssh: Optional[paramiko.SSHClient] = None
        self._conn_lock = threading.RLock()
        self._channel_sem = threading.BoundedSemaphore(max(1, config.max_channels))
        self._keepalive_stop = threading.Event()
        self._started = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------
    def start_keepalive(self) -> None:
        """Spawn the watchdog that rebuilds the transport after a drop."""
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._keepalive_loop, name="gw-keepalive",
                         daemon=True).start()

    def _keepalive_loop(self) -> None:
        while not self._keepalive_stop.is_set():
            try:
                self.ensure_connected()
                t = self._ssh.get_transport() if self._ssh else None
                if t is not None and t.is_active():
                    t.send_ignore()
            except Exception as e:
                log.warning("keepalive error: %s", e)
            self._keepalive_stop.wait(self.cfg.keepalive_interval)

    def _is_connected(self) -> bool:
        if self._ssh is None:
            return False
        t = self._ssh.get_transport()
        return t is not None and t.is_active()

    def connected(self) -> bool:
        return self._is_connected()

    def _host_key_policy(self) -> paramiko.MissingHostKeyPolicy:
        if self.cfg.known_hosts:
            path = os.path.expanduser(self.cfg.known_hosts)
            if os.path.exists(path):
                return _StrictHostKeyPolicy(path)
            log.warning("SSHGW_KNOWN_HOSTS=%s not found, falling back to AutoAdd", path)
        log.warning("SSHGW_KNOWN_HOSTS unset: accepting unknown host keys")
        return paramiko.AutoAddPolicy()

    def _new_client(self) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(self._host_key_policy())
        kwargs: dict = dict(
            hostname=self.cfg.remote_host,
            port=self.cfg.remote_port,
            username=self.cfg.remote_user,
            timeout=self.cfg.connect_timeout,
        )
        if self.cfg.remote_key_file:
            kwargs["key_filename"] = os.path.expanduser(self.cfg.remote_key_file)
        if self.cfg.remote_pass:
            kwargs["password"] = self.cfg.remote_pass
        client.connect(**kwargs)
        return client

    def ensure_connected(self) -> None:
        """Bring the transport up if it is down, with bounded retries.

        Only transient failures are retried. A refused password or an untrusted
        host key will fail identically on every attempt, so retrying just makes
        the client wait.
        """
        with self._conn_lock:
            if self._is_connected():
                return
            last_err: Optional[Exception] = None
            for attempt in range(1, self.cfg.reconnect_max_retry + 1):
                try:
                    self._ssh = self._new_client()
                    log.info("SSH transport up -> %s:%s",
                             self.cfg.remote_host, self.cfg.remote_port)
                    return
                except (HostKeyNotTrusted, paramiko.AuthenticationException) as e:
                    raise RuntimeError(f"SSH target refused the connection: {e}") from e
                except Exception as e:
                    last_err = e
                    log.warning("SSH connect failed (attempt %s/%s): %s", attempt,
                                self.cfg.reconnect_max_retry, e)
                    time.sleep(1 * attempt)
            raise RuntimeError(f"cannot reach SSH target: {last_err}")

    def close(self) -> None:
        self._keepalive_stop.set()
        with self._conn_lock:
            if self._ssh is not None:
                try:
                    self._ssh.close()
                except Exception:
                    pass
                self._ssh = None

    # ------------------------------------------------------------------
    # Command composition
    # ------------------------------------------------------------------
    def _build(self, cmd: str, cwd: str = "", with_pid: bool = False) -> str:
        """Compose the remote command line.

        ``with_pid`` wraps everything in ``exec <shell> -c 'echo $$; <cmd>'``:
        exec replaces the session shell, so $$ is the process that actually
        runs the command and the reported PID does not drift. The shell is
        configurable and defaults to ``sh`` because busybox targets (alpine
        containers, routers, recovery images) have no bash, while ``sh`` still
        handles for/if/function compound commands.
        """
        shell = self.cfg.remote_shell or "sh"
        if not with_pid:
            parts = [f"cd {shlex.quote(cwd)}"] if cwd else []
            parts.append(cmd)
            return " && ".join(parts)

        exec_ = f"exec {shell} -c {shlex.quote(f'echo $$ ; {cmd}')}"
        return f"cd {shlex.quote(cwd)} && {exec_}" if cwd else exec_

    @contextlib.contextmanager
    def _channel(self):
        """Hold one channel slot, or raise ChannelBusy after the queue timeout."""
        if not self._channel_sem.acquire(timeout=self.cfg.channel_queue_timeout):
            raise ChannelBusy(f"all {self.cfg.max_channels} SSH channels are busy")
        try:
            yield
        finally:
            self._channel_sem.release()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------
    def exec(self, cmd: str, cwd: str = "", timeout: Optional[float] = None) -> ExecResult:
        """Run a command to completion and return its result."""
        full = self._build(cmd, cwd, with_pid=False)
        self.ensure_connected()
        start = time.time()
        with self._channel():
            assert self._ssh is not None
            _in, out, err = self._ssh.exec_command(full, timeout=timeout or None)
            out_txt = out.read().decode(errors="replace")
            err_txt = err.read().decode(errors="replace")
            code = out.channel.recv_exit_status()
        return ExecResult(cmd=cmd, stdout=out_txt, stderr=err_txt, exit_code=code,
                          duration_ms=int((time.time() - start) * 1000))

    def exec_stream(self, cmd: str, cwd: str, on_event: Callable[[str, str], None],
                    on_exit: Optional[ExitCallback] = None,
                    timeout: Optional[float] = None) -> ExecResult:
        """Run a command, pushing each output chunk through ``on_event``."""
        full = self._build(cmd, cwd, with_pid=False)
        self.ensure_connected()
        start = time.time()
        collected: dict[str, list[str]] = {"out": [], "err": []}
        with self._channel():
            assert self._ssh is not None
            chan = self._ssh.get_transport().open_session()
            chan.settimeout(timeout or None)
            try:
                chan.exec_command(full)
            except Exception:
                chan.close()
                raise

            def reader(kind: str) -> None:
                while True:
                    data = chan.recv(8192) if kind == "out" else chan.recv_stderr(8192)
                    if not data:
                        break
                    text = data.decode(errors="replace")
                    collected[kind].append(text)
                    on_event("stdout" if kind == "out" else "stderr", text)

            threads = [threading.Thread(target=reader, args=(k,), daemon=True)
                       for k in ("out", "err")]
            for t_ in threads:
                t_.start()
            for t_ in threads:
                t_.join()
            code = chan.recv_exit_status()
            chan.close()
        if on_exit:
            on_exit(code)
        return ExecResult(cmd=cmd, stdout="".join(collected["out"]),
                          stderr="".join(collected["err"]), exit_code=code,
                          duration_ms=int((time.time() - start) * 1000))

    # ------------------------------------------------------------------
    # Detached processes (jobs)
    # ------------------------------------------------------------------
    def spawn(self, cmd: str, cwd: str = "",
              on_stdout: StreamCallback | None = None,
              on_stderr: StreamCallback | None = None) -> _RemoteProcess:
        """Start ``cmd`` detached on its own channel and return its handle."""
        full = self._build(cmd, cwd, with_pid=True)
        self.ensure_connected()
        if not self._channel_sem.acquire(timeout=self.cfg.channel_queue_timeout):
            raise ChannelBusy(f"all {self.cfg.max_channels} SSH channels are busy")
        try:
            assert self._ssh is not None
            chan = self._ssh.get_transport().open_session()
            chan.exec_command(full)
        except Exception:
            self._channel_sem.release()
            raise
        proc = _RemoteProcess(self, chan, on_stdout, on_stderr, want_pid=True,
                              on_finish=self._channel_sem.release)
        proc.start()
        return proc

    def kill_pid(self, pid: int) -> None:
        """TERM first, then KILL if the process survived."""
        self.exec(f"kill -TERM {int(pid)} 2>/dev/null; sleep 1; "
                  f"kill -9 {int(pid)} 2>/dev/null; true", timeout=20)

    # ------------------------------------------------------------------
    # SFTP
    # ------------------------------------------------------------------
    def sftp(self) -> paramiko.SFTPClient:
        self.ensure_connected()
        with self._conn_lock:
            assert self._ssh is not None
            return self._ssh.open_sftp()
