"""End-to-end tests against a real sshd in Docker.

Skipped unless ``SSHGW_TEST_DOCKER=1``: these need a container runtime, so the
default ``pytest`` run stays fast and portable. They are worth running because
the target is an Alpine box with **no bash** behind a real OpenSSH server --
the two things the in-process fakes cannot honestly attest to.

    SSHGW_TEST_DOCKER=1 pytest -m integration

Reuse a container you started yourself::

    SSHGW_TEST_DOCKER=1 SSHGW_TEST_SSHD_CONTAINER=gw-target pytest -m integration

On a host that cannot reach the Alpine CDN, point the image build at a mirror::

    SSHGW_TEST_DOCKER=1 SSHGW_TEST_APK_MIRROR=mirror.example/alpine pytest -m integration
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import threading
import time
import uuid

import pytest
from conftest import make_config, parse_sse

from ssh_gateway.center import GatewayCenter

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    os.environ.get("SSHGW_TEST_DOCKER") != "1",
    reason="set SSHGW_TEST_DOCKER=1 to run the Docker sshd tests")]

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sshd")
# Credentials of the container this file builds; override them only to point the
# suite at a container you started yourself.
DEFAULTS = {"user": "testuser", "password": "test-password", "home": "/home/testuser"}


def _docker(*args) -> str:
    proc = subprocess.run(["docker", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _target(port: int) -> dict:
    def get(key):
        return os.environ.get(f"SSHGW_TEST_SSHD_{key.upper()}", DEFAULTS[key])

    return {"host": "127.0.0.1", "port": port,
            "user": get("user"), "password": get("password"), "home": get("home")}


@pytest.fixture(scope="session")
def target():
    """A throwaway sshd container, or the caller's own if it is named."""
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")

    named = os.environ.get("SSHGW_TEST_SSHD_CONTAINER")
    if named:
        yield _target(int(_host_port(named)))
        return

    tag = f"ssh-gateway-test-target:{uuid.uuid4().hex[:8]}"
    name = f"ssh-gateway-test-{uuid.uuid4().hex[:8]}"
    build_args = ["build", "-t", tag]
    mirror = os.environ.get("SSHGW_TEST_APK_MIRROR")
    if mirror:
        build_args += ["--build-arg", f"APK_MIRROR={mirror}"]
    _docker(*build_args, FIXTURE_DIR)
    _docker("run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::2222", tag)
    try:
        port = int(_host_port(name))
        _wait_for_port(port)
        yield _target(port)
    finally:
        subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True)


@pytest.fixture(scope="session")
def home(target):
    return target["home"]


def _host_port(name: str) -> str:
    return _docker("inspect", "--format",
                   '{{(index (index .NetworkSettings.Ports "2222/tcp") 0).HostPort}}', name)


def _wait_for_port(port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.3)
    raise RuntimeError(f"sshd on port {port} never accepted a connection")


@pytest.fixture
def gw(target, tmp_path, serve, request):
    """A gateway on a real paramiko transport, plus its HTTP client."""
    cfg = make_config(tmp_path)
    cfg.remote_host, cfg.remote_port = target["host"], target["port"]
    cfg.remote_user, cfg.remote_pass = target["user"], target["password"]
    cfg.known_hosts = ""       # a fresh container regenerates its host key every build
    for marker in request.node.iter_markers("gw_config"):
        for key, value in marker.kwargs.items():
            setattr(cfg, key, value)

    center = GatewayCenter(config=cfg)
    center.start()
    try:
        if not request.node.get_closest_marker("gw_expect_unreachable"):
            _wait_until(center.backend.connected, timeout=20,
                        fail_msg="could not reach the test sshd")
        yield serve(center)
    finally:
        # Closing the transport matters: an abandoned keepalive thread would
        # still hold a session, and they stack up across tests.
        center.shutdown()


def _wait_until(predicate, timeout: float, fail_msg: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    pytest.fail(fail_msg)


def data(response) -> dict:
    """Unwrap one gateway envelope, failing with its error text."""
    payload = response.json()
    assert payload["ok"], payload["error"]
    return payload["data"]


def run(http, cmd, **kw):
    return data(http("POST", "/run", {"cmd": cmd, **kw}))["stdout"]


# ======================================================================
# Exec
# ======================================================================
def test_commands_run_without_bash(gw):
    """No bash on the target, and the gateway must still chain statements."""
    assert run(gw, "echo a; for i in 1 2; do printf '%s' $i; done; echo") == "a\n12\n"
    assert "NO_BASH" in run(gw, "command -v bash || echo NO_BASH")


def test_exit_code_and_stderr_are_reported(gw):
    r = gw("POST", "/run", {"cmd": "echo oops >&2; exit 3"})
    assert r.status == 200
    body = r.json()
    assert body["ok"] is True
    assert body["data"]["exit_code"] == 3
    assert "oops" in body["data"]["stderr"]
    assert body["data"]["duration_ms"] >= 0


def test_cwd_is_applied(gw, home):
    assert run(gw, "pwd", cwd=home).strip() == home


def test_stream_yields_real_output_as_sse(gw):
    r = gw("POST", "/run/stream", {"cmd": "printf one; sleep 1; printf two"})
    assert r.headers["Content-Type"].startswith("text/event-stream")
    frames = parse_sse(r.text)
    text = "".join(d["text"] for name, d in frames if name == "stdout")
    assert text == "onetwo"
    assert dict(frames)["exit"]["exit_code"] == 0


def test_health_reports_the_target(gw):
    assert data(gw("GET", "/health"))["connected"] is True


# ======================================================================
# Detached jobs, on a shell without job control
# ======================================================================
def job_until(gw, job_id, statuses=("done", "failed", "killed"), timeout=25.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = data(gw("GET", f"/job/{job_id}"))
        if job["status"] in statuses:
            return job
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} never reached {statuses}: {job}")


def test_job_completes_and_output_is_readable(gw):
    job_id = data(gw("POST", "/run/async", {"cmd": "sleep 1; echo jobdone"}))["job_id"]
    job = job_until(gw, job_id)
    assert job["status"] == "done"
    assert job["pid"], "the PID echo trick must work under a plain sh"
    assert "jobdone" in data(gw("GET", f"/job/{job_id}/output"))["tail"]


def test_job_kill_stops_the_remote_process(gw):
    job_id = data(gw("POST", "/run/async", {"cmd": "sleep 60; echo never"}))["job_id"]
    _wait_until(lambda: data(gw("GET", f"/job/{job_id}"))["pid"], 15,
                "no PID reported by the detached job")
    pid = data(gw("GET", f"/job/{job_id}"))["pid"]
    assert data(gw("POST", f"/job/{job_id}/kill"))["killed"] is True
    time.sleep(1.5)
    assert "GONE" in run(gw, f"ps -p {pid} >/dev/null 2>&1 && echo ALIVE || echo GONE")


def test_instantly_failing_job_does_not_wait_for_the_pid_grace(gw):
    """A command that dies at once must report failure, not stall 10s."""
    started = time.time()
    job_id = data(gw("POST", "/run/async", {"cmd": "exit 7"}))["job_id"]
    job = job_until(gw, job_id, statuses=("done", "failed", "killed"), timeout=6)
    assert job["status"] == "failed"
    assert time.time() - started < 5


# ======================================================================
# SFTP over a real server
# ======================================================================
def test_push_then_pull_is_incremental_both_ways(gw, tmp_path, home):
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("content-a")
    (src / "sub" / "b.txt").write_text("content-b")
    # A fixed, clearly-past mtime rather than "whatever write_text left":
    # against a current timestamp a no-op utime still passes whenever the
    # write and the upload land in the same second, which on a localhost
    # runner they usually do.
    stamp = 1_700_000_000
    for path in (src / "a.txt", src / "sub" / "b.txt"):
        os.utime(path, (stamp, stamp))
    remote = f"{home}/it-sync-{uuid.uuid4().hex[:6]}"

    def sync(**kw):
        return data(gw("POST", "/sftp/sync", kw))

    try:
        assert sync(direction="push", source=str(src), target=remote)["copied"] == 2
        # The mtime has to actually land on the far side, not merely be
        # *compared*: a no-op utime still looks incremental whenever the round
        # trip is shorter than the comparison tolerance, which is exactly what
        # a localhost CI runner is.
        far = run(gw, f"stat -c %Y {remote}/a.txt").strip()
        assert int(far) == stamp, "upload did not preserve the source mtime"
        again = sync(direction="push", source=str(src), target=remote)
        assert (again["copied"], again["skipped"]) == (0, 2), again["warnings"]

        down = tmp_path / "down"
        down.mkdir()
        assert sync(direction="pull", source=remote, target=str(down))["copied"] == 2
        assert (down / "a.txt").read_text() == "content-a"
        assert (down / "sub" / "b.txt").read_text() == "content-b"
        repull = sync(direction="pull", source=remote, target=str(down))
        assert (repull["copied"], repull["skipped"]) == (0, 2), repull["warnings"]
    finally:
        run(gw, f"rm -rf {remote}")


def test_pull_reads_the_remote_tree(gw, tmp_path, home):
    """Regression: pull used to resolve its source as a *local* path."""
    remote = f"{home}/pull-{uuid.uuid4().hex[:6]}"
    run(gw, f"mkdir -p {remote} && printf xyz > {remote}/only-remote.txt")
    into = tmp_path / "into"
    into.mkdir()
    try:
        stats = data(gw("POST", "/sftp/sync", {"direction": "pull", "source": remote,
                                               "target": str(into)}))
        assert stats["copied"] == 1
        assert (into / "only-remote.txt").read_text() == "xyz"
    finally:
        run(gw, f"rm -rf {remote}")


def test_pull_with_delete_prunes_local_excess(gw, tmp_path, home):
    remote = f"{home}/mirror-{uuid.uuid4().hex[:6]}"
    run(gw, f"mkdir -p {remote} && printf keep > {remote}/kept.txt")
    into = tmp_path / "into"
    into.mkdir()
    (into / "extra.txt").write_text("not upstream")
    try:
        stats = data(gw("POST", "/sftp/sync", {"direction": "pull", "source": remote,
                                               "target": str(into), "delete": True}))
        assert (stats["copied"], stats["deleted"]) == (1, 1)
        assert not (into / "extra.txt").exists()
        assert (into / "kept.txt").read_text() == "keep"
    finally:
        run(gw, f"rm -rf {remote}")


def test_list_reports_sizes_and_mtime(gw, home):
    items = {i["name"]: i
             for i in data(gw("POST", "/sftp/list", {"remote": f"{home}/data"}))["items"]}
    assert items["hello.txt"]["type"] == "file"
    assert items["hello.txt"]["mtime"]


def test_missing_remote_path_is_404(gw, home):
    assert gw("POST", "/sftp/list",
              {"remote": f"{home}/nope-{uuid.uuid4().hex[:6]}"}).status == 404


# ======================================================================
# Concurrency
# ======================================================================
def test_parallel_requests_get_their_own_response(gw):
    """The historical bug: a module-level "current handler" handed one client's
    bytes to another client's socket."""
    markers = [f"marker{i}" for i in range(8)]
    results: dict[str, str] = {}

    def worker(marker):
        results[marker] = run(gw, f"sleep 1; echo {marker}").strip()

    threads = [threading.Thread(target=worker, args=(m,)) for m in markers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {m: m for m in markers}


@pytest.mark.gw_config(max_channels=1, channel_queue_timeout=1)
def test_saturated_channel_pool_answers_503(gw):
    """One channel: the second caller queues, gives up, and is told to back off."""
    outcome: dict = {}

    def slow():
        outcome["first"] = gw("POST", "/run", {"cmd": "sleep 3; echo a"})

    thread = threading.Thread(target=slow)
    thread.start()
    time.sleep(0.5)
    second = gw("POST", "/run", {"cmd": "echo b"})
    thread.join()
    assert second.status == 503
    assert second.json()["code"] == "UNAVAILABLE"
    assert int(second.headers["Retry-After"]) > 0
    assert outcome["first"].json()["data"]["stdout"].strip() == "a"


# ======================================================================
# Host key verification
# ======================================================================
@pytest.mark.gw_config(known_hosts="/dev/null")
@pytest.mark.gw_expect_unreachable
def test_known_hosts_disables_trust_on_first_use(gw):
    """With SSHGW_KNOWN_HOSTS set, an unlisted host key must refuse the session."""
    r = gw("POST", "/run", {"cmd": "echo hi"})
    assert r.status == 502
    assert "host key" in r.json()["error"].lower()
