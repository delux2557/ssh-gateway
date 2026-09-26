"""The ``gw`` client's own argument parsing.

Nothing here opens a socket: these tests only pin down which spellings the
parser accepts, because that is where the client's ergonomics actually broke.
"""

from __future__ import annotations

import pytest

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
