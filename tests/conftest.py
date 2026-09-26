"""Shared test scaffolding: a fake SSH backend and a live in-process gateway.

Nothing here touches the network or a real device, so the whole suite runs in
CI without credentials. The Docker-backed tests in test_integration.py are the
ones that talk to a real sshd.
"""

from __future__ import annotations

import contextlib
import json
import os
import posixpath
import stat as statmod
import threading
import time
import urllib.error
import urllib.request

import pytest

from ssh_gateway.center import GatewayCenter
from ssh_gateway.config import Config
from ssh_gateway.models import ExecResult
from ssh_gateway.server import build_router, make_server


# ======================================================================
# Fake remote filesystem, speaking the paramiko.SFTPClient subset we use
# ======================================================================
class FakeAttr:
    def __init__(self, name: str, size: int, mtime: int, mode: int):
        self.filename = name
        self.st_size = size
        self.st_mtime = mtime
        self.st_mode = mode


class FakeSFTP:
    """Dict-backed SFTP server. ``fails_at`` injects per-path errors."""

    DIR_MODE = statmod.S_IFDIR | 0o755
    FILE_MODE = statmod.S_IFREG | 0o644

    def __init__(self, utime_supported: bool = True):
        # path -> [content(bytes), mtime(int)] or None for directories
        self.tree: dict[str, list] = {"/": [None, int(time.time())]}
        self.utime_supported = utime_supported
        self.utime_calls = 0

    # ---- helpers ----
    @staticmethod
    def _norm(path: str) -> str:
        return posixpath.normpath(path if path.startswith("/") else "/" + path)

    def _require(self, path: str) -> list:
        try:
            return self.tree[path]
        except KeyError:
            raise FileNotFoundError(f"No such file: {path}") from None

    def add_dir(self, path: str) -> None:
        self.tree[self._norm(path)] = [None, int(time.time())]
        parent = posixpath.dirname(self._norm(path))
        if parent and parent in self.tree and self.tree[parent][0] is not None:
            raise OSError("parent is a file")

    def add_file(self, path: str, content: str | bytes, mtime: int | None = None) -> None:
        path = self._norm(path)
        data = content.encode() if isinstance(content, str) else content
        parent = posixpath.dirname(path)
        if parent not in self.tree:
            raise FileNotFoundError(parent)
        self.tree[path] = [data, mtime if mtime is not None else int(time.time())]

    # ---- paramiko surface ----
    def stat(self, path: str) -> FakeAttr:
        entry = self._require(self._norm(path))
        name = posixpath.basename(self._norm(path)) or "/"
        if entry[0] is None:
            return FakeAttr(name, 4096, entry[1], self.DIR_MODE)
        return FakeAttr(name, len(entry[0]), entry[1], self.FILE_MODE)

    def listdir_attr(self, path: str) -> list[FakeAttr]:
        path = self._norm(path)
        if self._require(path)[0] is not None:
            raise OSError(f"Not a directory: {path}")
        out = []
        for child, entry in self.tree.items():
            if posixpath.dirname(child) == path and child != path:
                mode = self.DIR_MODE if entry[0] is None else self.FILE_MODE
                size = 4096 if entry[0] is None else len(entry[0])
                out.append(FakeAttr(posixpath.basename(child), size, entry[1], mode))
        return out

    def mkdir(self, path: str, mode: int = 0o755) -> None:
        path = self._norm(path)
        if path in self.tree:
            raise OSError(f"Failure: {path} exists")
        if posixpath.dirname(path) not in self.tree:
            raise OSError(f"parent missing for {path}")
        self.tree[path] = [None, int(time.time())]

    def put(self, local: str, remote: str) -> None:
        remote = self._norm(remote)
        if posixpath.dirname(remote) not in self.tree:
            raise OSError(f"no such directory: {posixpath.dirname(remote)}")
        with open(local, "rb") as f:
            self.tree[remote] = [f.read(), int(time.time())]

    def get(self, remote: str, local: str) -> None:
        self._get(remote, local)

    def _get(self, remote: str, local: str) -> None:
        with open(local, "wb") as f:
            f.write(self._require(self._norm(remote))[0])

    def remove(self, path: str) -> None:
        path = self._norm(path)
        self._require(path)
        del self.tree[path]

    def rmdir(self, path: str) -> None:
        path = self._norm(path)
        if any(posixpath.dirname(c) == path for c in self.tree):
            raise OSError("Directory not empty")
        del self.tree[path]

    def utime(self, path: str, attrs) -> None:
        self.utime_calls += 1
        if not self.utime_supported:
            raise OSError("Operation not supported")
        if isinstance(attrs, (tuple, list)):
            atime, mtime = attrs[0], attrs[1]
        else:
            atime = mtime = attrs
        # Mirror paramiko's rule instead of being kinder than it: the ACMODTIME
        # flag is only set when atime *and* mtime are present, so a None in
        # either slot sends no timestamps at all and the call succeeds anyway.
        # A fake that applied ``(None, mtime)`` hid exactly that bug.
        if atime is None or mtime is None:
            return
        path = self._norm(path)
        entry = self._require(path)
        entry[1] = int(mtime)


# ======================================================================
# Fake backend
# ======================================================================
class FakeProcess:
    """Stands in for a detached remote process."""

    def __init__(self, cmd: str, on_stdout=None, on_stderr=None,
                 pid: int = 4242, output: str = "", exit_code: int = 0,
                 runtime: float = 0.0):
        self.cmd = cmd
        self.pid = pid
        self.finished = False
        self.exit_code = exit_code
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self.output = output
        self.runtime = runtime

    def start(self) -> None:
        if self._on_stdout and self.output:
            self._on_stdout(self.output)

    def wait(self, timeout=None) -> int:
        if self.runtime:
            time.sleep(min(self.runtime, timeout or self.runtime))
        self.finished = True
        return self.exit_code


class FakeBackend:
    """Speaks the SSHBackend interface the center depends on."""

    def __init__(self, config: Config, sftp: FakeSFTP | None = None):
        self.cfg = config
        self.sftp_client = sftp if sftp is not None else FakeSFTP()
        self.connected_flag = True
        self.exec_error: Exception | None = None
        self.exec_calls: list[str] = []
        self.stream_output = ("out-1\n", "err-1\n")
        self.closed = 0

    # ---- lifecycle ----
    def start_keepalive(self) -> None:
        pass

    def ensure_connected(self) -> None:
        if self.exec_error:
            raise self.exec_error

    def connected(self) -> bool:
        return self.connected_flag

    def close(self) -> None:
        self.closed += 1
        self.connected_flag = False

    # ---- exec ----
    def _check(self) -> None:
        self.exec_calls.append("exec")
        if self.exec_error:
            raise self.exec_error

    def exec(self, cmd: str, cwd: str = "", timeout=None) -> ExecResult:
        self._check()
        return ExecResult(cmd=cmd, stdout=f"ok:{cmd}\n", stderr="", exit_code=0,
                          duration_ms=3)

    def exec_stream(self, cmd: str, cwd: str, on_event) -> ExecResult:
        self._check()
        out, err = self.stream_output
        if out:
            on_event("stdout", out)
        if err:
            on_event("stderr", err)
        return ExecResult(cmd=cmd, stdout=out, stderr=err, exit_code=0, duration_ms=5)

    def spawn(self, cmd: str, cwd: str = "", on_stdout=None, on_stderr=None) -> FakeProcess:
        self._check()
        proc = FakeProcess(cmd, on_stdout=on_stdout, on_stderr=on_stderr,
                           output=f"started:{cmd}\n")
        proc.start()  # SSHBackend.spawn returns an already-running handle
        return proc

    def kill_pid(self, pid: int) -> None:
        pass

    @contextlib.contextmanager
    def sftp(self):
        self._check()
        yield self.sftp_client


# ======================================================================
# Config / center / server fixtures
# ======================================================================
@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Never let the developer's own SSHGW_* shell settings leak into a test.

    SSHGW_TEST_* switches are the harness's own, so they survive the sweep.
    """
    for key in list(os.environ):
        if key.startswith("SSHGW_") and "_TEST_" not in key:
            monkeypatch.delenv(key, raising=False)


def make_config(tmp_path, **overrides) -> Config:
    cfg = Config()
    cfg.state_dir = str(tmp_path / "state")
    cfg.audit_file = str(tmp_path / "state" / "audit.jsonl")
    cfg.listen_host = "127.0.0.1"
    cfg.listen_port = 0            # ephemeral; read back from the server
    cfg.known_hosts = ""
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


class Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.headers = headers
        self._body = body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self) -> dict:
        return json.loads(self.text)


@pytest.fixture
def make_center(tmp_path):
    def _make(sftp: FakeSFTP | None = None, **config_overrides) -> GatewayCenter:
        cfg = make_config(tmp_path, **config_overrides)
        backend = FakeBackend(cfg, sftp=sftp)
        return GatewayCenter(config=cfg, backend=backend)

    return _make


@pytest.fixture
def serve():
    """``serve(center)`` -> a ``request(method, path, ...)`` callable.

    Handlers receive the center through a class attribute, so a test must not
    have two live servers at once.
    """
    started: list[tuple] = []

    def _serve(center: GatewayCenter):
        router = build_router(center)
        server = make_server(center, center.cfg, router)
        thread = threading.Thread(target=server.serve_forever,
                                 kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started.append((server, thread))
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def request(method: str, path: str, body=None, headers=None,
                    timeout: float = 15) -> Response:
            data = None
            hdrs = dict(headers or {})
            if body is not None:
                data = json.dumps(body).encode() if not isinstance(body, bytes) else body
                hdrs.setdefault("Content-Type", "application/json")
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers=hdrs)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload, status, rheaders = resp.read(), resp.status, dict(resp.headers)
            except urllib.error.HTTPError as e:
                payload, status, rheaders = e.read(), e.code, dict(e.headers)
            return Response(status, payload, rheaders)

        return request

    yield _serve

    for server, thread in started:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def center(make_center):
    return make_center()


@pytest.fixture
def api(serve, center):
    return serve(center)


def parse_sse(text: str) -> list[tuple[str, dict]]:
    """``event: x\\ndata: {...}`` frames -> [(name, payload)]."""
    frames = []
    for block in text.strip().split("\n\n"):
        name, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: "):]
            elif line.startswith("data: "):
                data += line[len("data: "):]
        if name:
            try:
                frames.append((name, json.loads(data)))
            except json.JSONDecodeError:
                frames.append((name, {"raw": data}))
    return frames


@pytest.fixture
def remote_tree():
    """A pre-populated fake remote filesystem."""
    sftp = FakeSFTP()
    sftp.add_dir("/remote")
    sftp.add_dir("/remote/sub")
    sftp.add_file("/remote/a.txt", "aaaa", mtime=1_700_000_000)
    sftp.add_file("/remote/sub/b.txt", "bb", mtime=1_700_000_001)
    return sftp
