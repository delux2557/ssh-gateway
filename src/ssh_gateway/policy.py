"""Command policy (policy.py)

Three admission modes, evaluated before anything touches the SSH channel:

  off        no filtering at all
  blacklist  deny the built-in disaster patterns plus any rule from the file
  whitelist  allow only rules from the file, and refuse shell control
             operators so an allowed prefix cannot be chained into anything
             else (``ls; rm -rf /`` must not pass an ``ls`` rule)

Read this as a guardrail against accidents and prompt-injection mischief,
**not** as a security boundary: a whitelisted program with an escape hatch
(``python3 -``, ``vim``, ``find -exec``) is still a shell. Real isolation has
to come from the remote account itself.
"""

from __future__ import annotations

import logging
import os
import re

log = logging.getLogger("ssh_gateway.policy")

# Operators that turn one permitted command into several arbitrary ones.
_CONTROL_OPS = (";", "&&", "||", "|", "`", "$(", "<(", ">(", "\n", "\r")


class CommandPolicy:
    def __init__(self, mode: str = "blacklist",
                 whitelist_file: str = "",
                 dangerous_patterns: list[str] | None = None,
                 allow_everything_when_no_whitelist: bool = False):
        self.mode = mode if mode in ("off", "whitelist", "blacklist") else "off"
        self._dangerous = [re.compile(p) for p in (dangerous_patterns or []) if p]
        self._rules: list[tuple[str, str]] = []  # (kind, value); kind = prefix|rx
        self._allow_without_rules = allow_everything_when_no_whitelist
        if whitelist_file:
            self._load_file(whitelist_file)
        if self.mode == "whitelist" and not self._rules:
            log.warning("policy mode is 'whitelist' but no rules were loaded: "
                        "everything will be denied"
                        if not self._allow_without_rules else
                        "policy mode is 'whitelist' with no rules and "
                        "SSHGW_ALLOW_EVERYTHING_WHEN_NO_WHITELIST=1: wide open")

    def _load_file(self, path: str) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"policy file not found: {path}")
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("rx:"):
                    self._rules.append(("rx", line[3:].strip()))
                else:
                    self._rules.append(("prefix", line))

    # ------------------------------------------------------------------
    def check(self, cmd: str) -> tuple[bool, str]:
        """Return (allowed, reason_when_denied)."""
        if self.mode == "off":
            return True, ""

        for pat in self._dangerous:
            if pat.search(cmd):
                return False, f"matches danger pattern: {pat.pattern}"

        if self.mode == "blacklist":
            for kind, value in self._rules:
                if (re.match(value, cmd) if kind == "rx" else cmd.startswith(value)):
                    return False, f"blocked by blacklist rule: {value}"
            return True, ""

        # whitelist
        for op in _CONTROL_OPS:
            if op in cmd:
                return False, (f"whitelist mode refuses shell operator {op!r}: "
                               f"prefix rules cannot constrain chained commands")
        if self._match_whitelist(cmd):
            return True, ""
        if not self._rules and self._allow_without_rules:
            return True, ""
        return False, ("command is not whitelisted" if self._rules
                       else "whitelist mode is on but no rules are configured")

    def _match_whitelist(self, cmd: str) -> bool:
        for kind, value in self._rules:
            if kind == "rx" and re.match(value, cmd):
                return True
            if kind == "prefix" and cmd.startswith(value):
                return True
        return False

    def describe(self) -> dict:
        return {"mode": self.mode, "rules": len(self._rules),
                "dangerous_rules": len(self._dangerous)}
