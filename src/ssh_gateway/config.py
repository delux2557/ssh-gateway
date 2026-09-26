"""Configuration (config.py)

Every knob comes from the environment (prefix ``SSHGW_``) or the CLI; nothing
about a specific device is baked in. If a required value is missing the
gateway refuses to start with an actionable message instead of silently
connecting somewhere unexpected.
"""

from __future__ import annotations

import ipaddress
import os


class ConfigError(Exception):
    """Raised when the configuration is unusable (missing target, no auth...)."""


def _env(name: str, default: str = "") -> str:
    # Only the SSHGW_ prefix is read: an unrelated $TOKEN or $REMOTE_PASS in the
    # environment must never turn into a credential.
    return os.environ.get("SSHGW_" + name, default)


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = _env(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _package_version() -> str:
    from ._version import __version__
    return __version__


def _is_loopback(host: str) -> bool:
    """True only for addresses no other machine can reach.

    Deliberately strict, and not a substring test: ``0.0.0.0``, ``::`` and the
    empty string all mean "every interface", while ``127.1.2.3`` and ``::1``
    mean the opposite. A hostname other than ``localhost`` is *not* assumed to
    be local, because DNS decides where it points.
    """
    name = (host or "").strip().lower()
    if name.startswith("[") and name.endswith("]"):     # bracketed IPv6 literal
        name = name[1:-1]
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _parse_target(spec: str) -> tuple[str, int, str]:
    """Parse ``[user@]host[:port]`` into (host, port, user)."""
    user, _, rest = spec.rpartition("@") if "@" in spec else ("", "", spec)
    host = rest
    port = 22
    if ":" in rest:
        host, _, raw_port = rest.rpartition(":")
        try:
            port = int(raw_port)
        except ValueError as e:
            raise ConfigError(f"SSHGW_TARGET port is not a number: {raw_port!r} in {spec!r}") from e
        if not 0 < port < 65536:
            raise ConfigError(f"SSHGW_TARGET port out of range: {spec!r}")
    return host, port, user


class Config:
    """Runtime settings. Plain attributes so tests can override single fields."""

    def __init__(self) -> None:
        # ---- HTTP listener ----
        self.listen_host: str = _env("LISTEN_HOST", "127.0.0.1")
        self.listen_port: int = _int("LISTEN_PORT", 8023)

        # ---- SSH target (no defaults on purpose) ----
        target = _env("TARGET")
        if target:
            self.remote_host, self.remote_port, self.remote_user = _parse_target(target)
        else:
            self.remote_host = _env("REMOTE_HOST")
            self.remote_port = _int("REMOTE_PORT", 22)
            self.remote_user = _env("REMOTE_USER")
        self.remote_pass: str = _env("REMOTE_PASS")
        self.remote_key_file: str = _env("REMOTE_KEY")
        # When set, unknown host keys are rejected instead of auto-added.
        self.known_hosts: str = _env("KNOWN_HOSTS")
        # Command execution uses a POSIX shell so the gateway works on busybox
        # targets (containers, routers, recovery images), not just bash hosts.
        self.remote_shell: str = _env("REMOTE_SHELL", "sh")
        self.connect_timeout: int = _int("CONNECT_TIMEOUT", 10)
        self.keepalive_interval: int = _int("KEEPALIVE_INTERVAL", 30)
        self.reconnect_max_retry: int = _int("RECONNECT_MAX_RETRY", 3)

        # ---- Concurrency ----
        # SSHd's default MaxSessions is 10; keep the gateway below that by
        # default so channels are queued instead of being refused by the peer.
        self.max_channels: int = _int("MAX_CHANNELS", 8)
        self.channel_queue_timeout: int = _int("CHANNEL_QUEUE_TIMEOUT", 30)

        # ---- Security ----
        self.api_token: str = _env("TOKEN")
        self.policy_mode: str = _env("POLICY_MODE", "blacklist").lower()
        self.whitelist_file: str = _env("WHITELIST_FILE")
        self.allow_everything_when_no_whitelist: bool = _bool(
            "ALLOW_EVERYTHING_WHEN_NO_WHITELIST", False)
        _extra = [p for p in _env("DANGEROUS").split(",") if p.strip()]
        self.dangerous_patterns: list[str] = [
            r"rm\s+-[a-z]*r[a-z]*f?\s+(-\S+\s+)*/(\s|$)",   # rm -rf / and flag variants
            r"rm\s+--no-preserve-root",
            r"mkfs\.",
            r"dd\s+if=.*\sof=/dev/[a-z]",
            r">\s*/dev/(sd|nvme|vd)[a-z]",
            r":\(\)\s*\{\s*:\|:&",                           # fork bomb
            r"\bshutdown\b", r"\breboot\b", r"\bkexec\b",
            r"\binit\s+0\b",
        ] + _extra

        # ---- Audit ----
        self.audit_enabled: bool = _bool("AUDIT", True)
        self.audit_file: str = _env("AUDIT_FILE")
        self.audit_max_entries: int = _int("AUDIT_MAX_ENTRIES", 2000)

        # ---- State / jobs ----
        self.state_dir: str = _env("STATE_DIR", os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            ".sshgw-state"))
        self.job_max_runtime: int = _int("JOB_MAX_RUNTIME", 60 * 60)

        # ---- TLS ----
        self.tls_enabled: bool = _bool("TLS", False)
        self.tls_cert: str = _env("TLS_CERT")
        self.tls_key: str = _env("TLS_KEY")

        # ---- Version (single source: the packaged version) ----
        self.version: str = _package_version()

    # ------------------------------------------------------------------
    def validate(self) -> None:
        """Fail fast on unusable configuration."""
        # Checked before anything else because it is the one setting whose
        # mistake is not a broken run but an open door: reachable from off-box
        # and unauthenticated, POST /run is a remote shell for anyone who can
        # open the port. Loopback needs no token (the local user already has a
        # shell); every other bind does, no exceptions.
        if not _is_loopback(self.listen_host) and not self.api_token:
            where = self.listen_host or "(every interface)"
            raise ConfigError(
                f"refusing to listen on {where} without SSHGW_TOKEN: /run would "
                "be an unauthenticated remote shell for anyone who can reach "
                "the port. Set SSHGW_TOKEN, or listen on 127.0.0.1 and reach "
                "the gateway through a tunnel")
        if not self.remote_host or not self.remote_user:
            raise ConfigError(
                "no SSH target: set SSHGW_TARGET=user@host[:port] "
                "(or SSHGW_REMOTE_HOST / SSHGW_REMOTE_USER / SSHGW_REMOTE_PORT)")
        if not self.remote_pass and not self.remote_key_file:
            raise ConfigError(
                "no SSH credentials: set SSHGW_REMOTE_PASS or SSHGW_REMOTE_KEY "
                "(password-less local accounts are not assumed)")
        if self.tls_enabled and not (self.tls_cert and self.tls_key):
            raise ConfigError("SSHGW_TLS=1 requires SSHGW_TLS_CERT and SSHGW_TLS_KEY")
        if self.policy_mode not in ("off", "whitelist", "blacklist"):
            raise ConfigError(
                f"SSHGW_POLICY_MODE must be off|whitelist|blacklist, got {self.policy_mode!r}")

    def ensure_dirs(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        if self.audit_enabled and not self.audit_file:
            self.audit_file = os.path.join(self.state_dir, "audit.jsonl")
        if self.job_max_runtime:
            os.makedirs(os.path.join(self.state_dir, "jobs"), exist_ok=True)

    def __str__(self) -> str:  # never includes secrets
        return (f"Config(listen={self.listen_host}:{self.listen_port}, "
                f"target={self.remote_user}@{self.remote_host}:{self.remote_port}, "
                f"auth={'token' if self.api_token else 'none'}, "
                f"policy={self.policy_mode}, tls={self.tls_enabled})")

    def safe_env_lines(self) -> list[str]:
        """Copy-pasteable invocation with secrets redacted."""
        return [
            f"SSHGW_TARGET={self.remote_user}@{self.remote_host}:{self.remote_port}",
            f"SSHGW_REMOTE_KEY={self.remote_key_file or '<unset>'}",
            f"SSHGW_REMOTE_PASS={'<set>' if self.remote_pass else '<unset>'}",
            f"SSHGW_TOKEN={'<set>' if self.api_token else '<unset>'}",
        ]
