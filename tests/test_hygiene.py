"""Repo hygiene: no personal or environment-specific data may ever be committed.

This project is meant to be published, and it started life as a private script
that knew one real device: its LAN address, its owner's paths, its access
token. Two kinds of check keep that from coming back:

* rules that hold for anybody — addresses must be loopback or RFC 5737
  documentation ranges, home directories must be fictional accounts, the
  current machine's own $HOME and hostname may not appear in any file;
* a denylist of strings only the original author knows about, which lives in
  ``.hygiene-denied``. That file is gitignored on purpose: publishing the
  check without publishing the private data it protects is the whole point.
"""

from __future__ import annotations

import os
import re
import socket

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: literal substrings that must never appear in a tracked file, for anybody
FORBIDDEN_STRINGS = {
    "com.termux": "Termux paths describe one person's phone, not a supported target",
    "termux": "Termux paths describe one person's phone, not a supported target",
    "sdcard": "Termux paths describe one person's phone, not a supported target",
    "BEGIN OPENSSH PRIVATE KEY": "private key material",
    "BEGIN RSA PRIVATE KEY": "private key material",
    "BEGIN PRIVATE KEY": "private key material",
    "ssh-ed25519 AAAA": "public key material belongs in ~/.ssh, not the repo",
    "ghp_": "a GitHub personal access token",
    "github_pat_": "a GitHub fine-grained token",
}

#: these file types are never source, whatever their contents
FORBIDDEN_SUFFIXES = (".key", ".pem", ".p12", ".pfx", ".jsonl", ".kdbx", ".env")

#: Structural rules, so the guarantee does not depend on knowing which strings
#: were personal last time: any address in this repository has to be loopback
#: or one of the documentation ranges, and any home directory has to be a
#: fictional account.
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
ALLOWED_ADDRESS_PREFIXES = ("127.", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.")
HOME_PATH = re.compile(r"(?:/home/|/Users/)([A-Za-z0-9._$-]+)")
WINDOWS_HOME = re.compile(r"[A-Za-z]:[\\/]{1,2}Users[\\/]([^\\/]+)")
FICTIONAL_ACCOUNTS = {"testuser", "x", "user", "someone", "deploy", "$USER"}

#: each contributor's own private strings, never committed
LOCAL_DENYLIST = os.path.join(REPO_ROOT, ".hygiene-denied")

SKIP_DIRS = {".git", ".hg", "node_modules", "__pycache__", ".sshgw-state",
             ".pytest_cache", ".ruff_cache", "build", "dist", ".venv", "venv"}

SELF = os.path.abspath(__file__)

#: this file and the local denylist quote the rules by design
EXEMPT = {SELF, os.path.abspath(LOCAL_DENYLIST)}


def local_denied():
    """The current machine's identifiers, plus anything .hygiene-denied lists."""
    denied = {}
    home = os.path.expanduser("~")
    if home and home != "/":
        denied[home] = "the home directory of whoever is running the tests"
    host = socket.gethostname()
    if host:
        denied[host] = "the hostname of whoever is running the tests"
    try:
        with open(LOCAL_DENYLIST, encoding="utf-8") as handle:
            for line in handle:
                needle = line.strip()
                if needle and not needle.startswith("#"):
                    denied[needle] = "listed in .hygiene-denied"
    except OSError:
        pass                      # no local denylist: the general rules still run
    return denied


def source_files():
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            path = os.path.abspath(os.path.join(root, name))
            if path in EXEMPT:
                continue
            yield path, name


def read_if_text(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    if b"\0" in blob[:4096]:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


@pytest.mark.parametrize("path, name", sorted(source_files()),
                         ids=lambda value: os.path.relpath(value, REPO_ROOT)
                         if isinstance(value, str) else str(value))
def test_no_forbidden_file_types(path, name):
    rel = os.path.relpath(path, REPO_ROOT)
    assert not rel.endswith("/.env"), f"{rel}: local environment file must not be committed"
    lowered = name.lower()
    assert not lowered.endswith(FORBIDDEN_SUFFIXES), \
        f"{rel}: {lowered.rsplit('.', 1)[-1]} material must never be committed"


@pytest.mark.parametrize("path, name", sorted(source_files()))
def test_no_personal_or_local_data(path, name):
    text = read_if_text(path)
    if text is None:
        return
    rel = os.path.relpath(path, REPO_ROOT)
    lowered = text.lower()

    for needle, why in FORBIDDEN_STRINGS.items():
        assert needle.lower() not in lowered, f"{rel} contains {needle!r}: {why}"

    for needle, why in local_denied().items():
        assert needle.lower() not in lowered, f"{rel} contains {needle!r}: {why}"

    for address in IPV4.findall(text):
        assert address.startswith(ALLOWED_ADDRESS_PREFIXES), (
            f"{rel} contains {address}, which is neither loopback nor an RFC 5737 "
            "documentation address")

    for account in HOME_PATH.findall(text) + WINDOWS_HOME.findall(text):
        assert account in FICTIONAL_ACCOUNTS, (
            f"{rel} points at a real-looking account ({account!r}); use one of "
            f"{sorted(FICTIONAL_ACCOUNTS)}")
