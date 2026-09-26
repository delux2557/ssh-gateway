"""The ``gw`` client's argument parsing and its exit status.

Nothing here opens a socket. The parser tests pin down which spellings the
client accepts, because that is where its ergonomics actually broke; the
``_request`` patch below replaces the transport with a canned envelope so the
status the process returns can be asserted without a gateway to talk to.
"""

from __future__ import annotations

import pytest

from ssh_gateway import client
from ssh_gateway.client import build_parser


@pytest.mark.parametrize("argv", [
    ["--json", "status"], ["status", "--json"],
    ["--json", "jobs"], ["jobs", "--json"],
    ["--json", "job", "abc"], ["job", "abc", "--json"],
    ["--json", "output", "abc"], ["output", "abc", "--json"],
    ["output", "abc", "--tail", "50", "--json"],
    ["--json", "kill", "abc"], ["kill", "abc", "--json"],
    ["--json", "run", "true"], ["run", "true", "--json"],
    ["stream", "true", "--json"], ["async", "true", "--json"],
    ["--json", "sftp", "ls", "/tmp"], ["sftp", "ls", "/tmp", "--json"],
    ["--json", "routes"], ["routes", "--json"],
])
def test_json_is_accepted_on_either_side_of_the_verb(argv):
    """``gw status --json`` has to parse.

    While ``--json`` was a global-only option, that spelling died in argparse
    with a bare "unrecognized arguments: --json" -- so the flag every caller
    was told to always pass was the one it could not append.
    """
    assert build_parser().parse_args(argv).json is True


@pytest.mark.parametrize("argv", [
    ["status"], ["jobs"], ["routes"], ["run", "true"], ["sftp", "ls", "/tmp"],
])
def test_json_stays_off_unless_asked_for(argv):
    assert build_parser().parse_args(argv).json is False


def test_global_options_survive_the_subparser():
    """The parent parser must not disturb what the top-level one recorded.

    A subparser parses its slice of the argv into a fresh namespace and copies
    every attribute over the outer one, so re-declaring ``--json`` with an
    ordinary default would reset ``gw --json status`` to non-JSON. The shared
    declaration therefore suppresses its default; this is the regression guard.
    """
    args = build_parser().parse_args(
        ["--url", "http://127.0.0.1:9", "--token", "t", "--json", "status"])
    assert (args.url, args.token, args.json) == ("http://127.0.0.1:9", "t", True)
    assert args.func.__name__ == "cmd_status"


def test_a_missing_subcommand_is_a_usage_error():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


# ----------------------------------------------------------------------
# The exit status a caller's shell acts on. ``gw run --json`` returned 0 for a
# failing command, which quietly disabled the one signal the verb exists to
# deliver -- and did it for exactly the callers the README tells to pass --json.
# ----------------------------------------------------------------------
def _canned(monkeypatch, data):
    monkeypatch.setattr(client, "_request", lambda *a, **k: (200, {"ok": True, "data": data}))


@pytest.mark.parametrize("argv", [
    ["run", "--json", "exit 7"],
    ["--json", "run", "exit 7"],
    ["run", "exit 7"],
])
def test_a_failing_command_exits_nonzero_however_it_is_printed(monkeypatch, capsys, argv):
    _canned(monkeypatch, {"stdout": "", "stderr": "", "exit_code": 7})
    assert client.main(argv) == 7


def test_a_passing_command_exits_zero(monkeypatch, capsys):
    _canned(monkeypatch, {"stdout": "fine\n", "stderr": "", "exit_code": 0})
    assert client.main(["run", "--json", "true"]) == 0


def test_a_payload_without_an_exit_code_does_not_invent_one(monkeypatch, capsys):
    """``status`` carries transport state, not a command result."""
    _canned(monkeypatch, {"connected": True, "jobs_running": 0})
    assert client.main(["status", "--json"]) == 0


def test_json_still_prints_the_payload_while_reporting_the_status(monkeypatch, capsys):
    """Both halves matter: the format changes, the status does not disappear."""
    _canned(monkeypatch, {"stdout": "", "stderr": "", "exit_code": 3})
    assert client.main(["run", "--json", "false"]) == 3
    out = capsys.readouterr().out
    assert '"exit_code": 3' in out
