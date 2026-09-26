"""Command policy: the guardrail in front of the SSH channel."""

from __future__ import annotations

import pytest

from ssh_gateway.config import Config
from ssh_gateway.policy import CommandPolicy

MASS_DESTRUCTION = [
    "rm -rf / --no-preserve-root",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    ":(){ :|:& };:",
    "shutdown -h now",
    "init 0",
]


def blacklist(**kw) -> CommandPolicy:
    cfg = Config()
    return CommandPolicy(mode="blacklist", dangerous_patterns=cfg.dangerous_patterns,
                         allow_everything_when_no_whitelist=True, **kw)


# ----------------------------------------------------------------------
def test_off_mode_allows_everything():
    policy = CommandPolicy(mode="off")
    assert policy.check("rm -rf /")[0] is True
    assert policy.describe() == {"mode": "off", "rules": 0, "dangerous_rules": 0}


def test_unknown_mode_degrades_to_off():
    assert CommandPolicy(mode="typo").mode == "off"


@pytest.mark.parametrize("cmd", MASS_DESTRUCTION)
def test_default_danger_patterns_are_blocked(cmd):
    allowed, reason = blacklist().check(cmd)
    assert allowed is False
    assert "danger pattern" in reason


def test_harmless_commands_pass_blacklist():
    for cmd in ["ls -la", "df -h", "git status", "cat /proc/meminfo"]:
        assert blacklist().check(cmd)[0] is True


def test_blacklist_file_rules_are_enforced(tmp_path):
    rules = tmp_path / "bl.txt"
    rules.write_text("# a comment\nrm \nrx:^curl .*file://\n")
    policy = blacklist(whitelist_file=str(rules))
    assert policy.check("rm stuff")[0] is False
    assert policy.check("curl file:///etc/passwd")[0] is False
    assert policy.check("ls")[0] is True
    assert policy.describe()["rules"] == 2


def test_missing_policy_file_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        CommandPolicy(mode="whitelist", whitelist_file=str(tmp_path / "nope.txt"))


# ----------------------------------------------------------------------
# Whitelist
# ----------------------------------------------------------------------
def make_whitelist(tmp_path, *rules) -> CommandPolicy:
    path = tmp_path / "wl.txt"
    path.write_text("\n".join(rules))
    return CommandPolicy(mode="whitelist", whitelist_file=str(path),
                         dangerous_patterns=Config().dangerous_patterns)


def test_whitelist_allows_listed_prefixes(tmp_path):
    policy = make_whitelist(tmp_path, "ls", "df")
    assert policy.check("ls -la")[0] is True
    assert policy.check("df -h")[0] is True


def test_whitelist_denies_unlisted(tmp_path):
    allowed, reason = make_whitelist(tmp_path, "ls").check("wget evil")
    assert allowed is False
    assert "not whitelisted" in reason


@pytest.mark.parametrize("op", [";", "&&", "||", "|", "`", "$(", "<(", ">(", "\n"])
def test_whitelist_refuses_shell_operators(tmp_path, op):
    """An ``ls`` prefix rule must not become ``ls`` plus anything else."""
    policy = make_whitelist(tmp_path, "ls")
    allowed, reason = policy.check(f"ls {op} echo hi")
    assert allowed is False
    assert "shell operator" in reason


def test_whitelist_without_rules_denies_by_default(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    policy = CommandPolicy(mode="whitelist", whitelist_file=str(empty))
    allowed, reason = policy.check("ls")
    assert allowed is False
    assert "no rules are configured" in reason


def test_whitelist_without_rules_can_opt_into_open(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    policy = CommandPolicy(mode="whitelist", whitelist_file=str(empty),
                           allow_everything_when_no_whitelist=True)
    assert policy.check("anything")[0] is True


def test_whitelist_rules_are_prefixes_not_words(tmp_path):
    """Documented limitation: an ``ls`` rule also matches ``lsof``.

    Rules are prefixes of the command line, so they have to be written with a
    trailing space (or as ``rx:``) to mean "this program only".
    """
    loose = make_whitelist(tmp_path, "ls")
    assert loose.check("lsof -i")[0] is True
    strict = make_whitelist(tmp_path, "rx:^ls(\\s|$)")
    assert strict.check("ls -l")[0] is True
    assert strict.check("lsof -i")[0] is False


def test_danger_patterns_beat_the_whitelist(tmp_path):
    policy = make_whitelist(tmp_path, "rm")
    assert policy.check("rm -rf / --no-preserve-root")[0] is False
