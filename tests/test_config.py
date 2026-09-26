"""Configuration is env-only and carries no device identity."""

from __future__ import annotations

import pytest

from ssh_gateway.config import Config, ConfigError


@pytest.fixture
def env(monkeypatch):
    def _set(**values):
        for key, value in values.items():
            monkeypatch.setenv("SSHGW_" + key, str(value))
    return _set


# ----------------------------------------------------------------------
def test_nothing_is_baked_in_by_default():
    cfg = Config()
    assert cfg.remote_host == ""
    assert cfg.remote_user == ""
    assert cfg.remote_pass == ""
    assert cfg.api_token == ""
    assert cfg.listen_host == "127.0.0.1"        # not 0.0.0.0
    assert cfg.known_hosts == ""
    with pytest.raises(ConfigError, match="SSHGW_TARGET"):
        cfg.validate()


def test_missing_credentials_are_rejected(env):
    env(TARGET="someone@example.invalid")
    cfg = Config()
    assert cfg.remote_user == "someone"
    assert cfg.remote_host == "example.invalid"
    assert cfg.remote_port == 22
    with pytest.raises(ConfigError, match="credentials"):
        cfg.validate()


@pytest.mark.parametrize("spec,expected", [
    ("example.invalid", ("example.invalid", 22, "")),
    ("someone@example.invalid", ("example.invalid", 22, "someone")),
    ("someone@example.invalid:2222", ("example.invalid", 2222, "someone")),
    ("root@203.0.113.5:2200", ("203.0.113.5", 2200, "root")),
])
def test_target_parsing(env, spec, expected):
    env(TARGET=spec, REMOTE_PASS="x")
    cfg = Config()
    assert (cfg.remote_host, cfg.remote_port, cfg.remote_user) == expected


@pytest.mark.parametrize("spec", ["u@host:notaport", "u@host:0", "u@host:99999"])
def test_bad_target_port_fails_loudly(env, spec):
    env(TARGET=spec)
    with pytest.raises(ConfigError, match="port"):
        Config()


def test_separate_variables_win_over_no_target(env):
    env(REMOTE_HOST="host.internal", REMOTE_USER="someone", REMOTE_PORT="2200",
       REMOTE_KEY="/home/x/.ssh/id_ed25519")
    cfg = Config()
    cfg.validate()
    assert (cfg.remote_host, cfg.remote_port, cfg.remote_user) == ("host.internal", 2200, "someone")


def test_tls_without_material_is_rejected(env):
    env(TARGET="someone@host", REMOTE_PASS="x", TLS="1")
    with pytest.raises(ConfigError, match="TLS"):
        Config().validate()


def test_bad_policy_mode_is_rejected(env):
    env(TARGET="someone@host", REMOTE_PASS="x", POLICY_MODE="maybe")
    with pytest.raises(ConfigError, match="POLICY_MODE"):
        Config().validate()


# ----------------------------------------------------------------------
# The one setting whose mistake is an open door rather than a broken run.
@pytest.mark.parametrize("listen", [
    "0.0.0.0",          # every IPv4 interface
    "::",               # every IPv6 interface
    "192.0.2.9",        # a routable-looking address
    "",                 # what HTTPServer reads as "every interface"
    "gateway.lan",      # a name, so DNS decides where it points
])
def test_exposed_listener_without_a_token_is_refused(env, listen):
    """Off-box and unauthenticated means POST /run is a shell for anyone.

    The empty string belongs in this list: ``SSHGW_LISTEN_HOST=`` reads as
    unset but binds to every interface, which is the opposite of loopback.
    """
    env(TARGET="someone@host", REMOTE_PASS="x", LISTEN_HOST=listen)
    with pytest.raises(ConfigError, match="SSHGW_TOKEN"):
        Config().validate()


@pytest.mark.parametrize("listen", [
    "127.0.0.1", "127.5.5.5", "localhost", "LOCALHOST", "::1", "[::1]",
])
def test_loopback_listener_needs_no_token(env, listen):
    """Anyone who can reach loopback already has a shell on this machine."""
    env(TARGET="someone@host", REMOTE_PASS="x", LISTEN_HOST=listen)
    Config().validate()


@pytest.mark.parametrize("listen", ["0.0.0.0", "192.0.2.9", "gateway.lan"])
def test_exposed_listener_is_allowed_once_a_token_is_set(env, listen):
    env(TARGET="someone@host", REMOTE_PASS="x", LISTEN_HOST=listen, TOKEN="s3cret")
    Config().validate()


def test_a_hostname_that_mimics_loopback_is_still_exposed(env):
    """``127.example.test`` is a name, not the loopback range: a substring test
    would wave it through, ipaddress does not."""
    env(TARGET="someone@host", REMOTE_PASS="x", LISTEN_HOST="127.example.test")
    with pytest.raises(ConfigError, match="SSHGW_TOKEN"):
        Config().validate()


def test_unprefixed_variables_are_ignored(monkeypatch):
    """A stray $TOKEN in the environment must not become an auth secret."""
    monkeypatch.setenv("TOKEN", "accidental")
    monkeypatch.setenv("REMOTE_PASS", "accidental")
    cfg = Config()
    assert cfg.api_token == ""
    assert cfg.remote_pass == ""


# ----------------------------------------------------------------------
def test_summary_never_prints_secrets(env):
    env(TARGET="someone@host:2222", REMOTE_PASS="hunter2", TOKEN="s3cret")
    cfg = Config()
    assert "hunter2" not in str(cfg)
    assert "s3cret" not in str(cfg)
    joined = "\n".join(cfg.safe_env_lines())
    assert "hunter2" not in joined and "s3cret" not in joined
    assert "SSHGW_REMOTE_PASS=<set>" in joined


def test_extra_dangerous_patterns_are_appended(env):
    env(DANGEROUS="^sudo ,reboot now")
    cfg = Config()
    assert cfg.dangerous_patterns[-2:] == ["^sudo ", "reboot now"]
    assert len(cfg.dangerous_patterns) > 2


def test_bool_and_int_parsing(env):
    env(TLS="TRUE", AUDIT="off", MAX_CHANNELS="abc", KEEPALIVE_INTERVAL="15")
    cfg = Config()
    assert cfg.tls_enabled is True
    assert cfg.audit_enabled is False
    assert cfg.max_channels == 8        # garbage falls back to the default
    assert cfg.keepalive_interval == 15


def test_ensure_dirs_creates_state_layout(tmp_path, env):
    env(STATE_DIR=str(tmp_path / "s"), TARGET="someone@host", REMOTE_PASS="x")
    cfg = Config()
    cfg.ensure_dirs()
    assert (tmp_path / "s" / "jobs").is_dir()
    assert cfg.audit_file == str(tmp_path / "s" / "audit.jsonl")


def test_version_matches_the_package():
    from ssh_gateway import _version
    assert Config().version == _version.__version__
