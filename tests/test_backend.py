"""Backend pieces that do not need a socket: command building, PID handling."""

from __future__ import annotations

import shlex

import paramiko
import pytest
from conftest import make_config

from ssh_gateway.backend import SSHBackend, _StrictHostKeyPolicy


@pytest.fixture
def make_backend(tmp_path):
    def _make(**overrides) -> SSHBackend:
        return SSHBackend(make_config(tmp_path, **overrides))

    return _make


# ----------------------------------------------------------------------
def test_bash_is_not_required(make_backend):
    """The original shipped a hardcoded ``bash -c``; busybox targets have no bash."""
    backend = make_backend()
    assert backend.cfg.remote_shell == "sh"
    assert backend._build("echo hi") == "echo hi"
    assert "bash" not in backend._build("echo hi", with_pid=True)


def test_configured_shell_is_used_for_detached_jobs(make_backend):
    line = make_backend(remote_shell="ash")._build("sleep 1", with_pid=True)
    assert line.startswith("exec ash -c ")
    assert shlex.split(line)[-1] == "echo $$ ; sleep 1"


def test_cwd_is_quoted_and_applied(make_backend):
    line = make_backend()._build("ls -l", cwd="/tmp/a b'c")
    assert line == "cd " + shlex.quote("/tmp/a b'c") + " && ls -l"


def test_the_pid_wrapper_replaces_the_session_shell(make_backend):
    """``exec sh -c 'echo $$'`` makes $$ the PID of the command, not of sshd's shell."""
    line = make_backend()._build("true", with_pid=True)
    assert line == "exec sh -c " + shlex.quote("echo $$ ; true")


def test_detached_job_cwd_precedes_the_wrapper(make_backend):
    line = make_backend()._build("sleep 1", cwd="/tmp/work", with_pid=True)
    assert line.startswith("cd /tmp/work && exec sh -c ")


# ----------------------------------------------------------------------
def test_strict_policy_refuses_unknown_keys(tmp_path):
    known = tmp_path / "known_hosts"
    known.write_text("")
    with pytest.raises(paramiko.SSHException) as exc:
        _StrictHostKeyPolicy(str(known)).missing_host_key(None, "example.invalid", object())
    # the message must name the file the operator has to edit
    assert str(known) in str(exc.value)


def test_strict_policy_accepts_a_comment_only_file(tmp_path):
    path = tmp_path / "kh"
    path.write_text("# only a comment\n")
    _StrictHostKeyPolicy(str(path))


# ----------------------------------------------------------------------
def test_kill_sequence_terms_before_killing(make_backend, monkeypatch):
    backend = make_backend()
    calls = []
    monkeypatch.setattr(backend, "ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "exec", lambda cmd, cwd="", timeout=None: calls.append(cmd))
    backend.kill_pid(4321)
    assert calls and "kill -TERM 4321" in calls[0] and "kill -9 4321" in calls[0]


def test_a_pid_cannot_smuggle_extra_commands(make_backend, monkeypatch):
    """The PID is interpolated into a shell string: it must stay an integer."""
    backend = make_backend()
    seen = []
    monkeypatch.setattr(backend, "ensure_connected", lambda: None)
    monkeypatch.setattr(backend, "exec", lambda cmd, cwd="", timeout=None: seen.append(cmd))
    with pytest.raises((TypeError, ValueError)):
        backend.kill_pid("1; rm -rf /")
    assert seen == []
