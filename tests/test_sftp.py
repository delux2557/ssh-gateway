"""SFTP service against an in-memory remote filesystem.

These are the regressions for the two original bugs: ``pull`` treated its
arguments as ``push`` did, and incremental sync never skipped because mtimes
were not carried across.
"""

from __future__ import annotations

import os
import time

import pytest
from conftest import FakeSFTP, make_config

from ssh_gateway.sftp import SFTPService

T0 = 1_700_000_000


@pytest.fixture
def local_tree(tmp_path):
    root = tmp_path / "loc"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("aaaa")
    (root / "sub" / "b.txt").write_text("bb")
    for path in (root / "a.txt", root / "sub" / "b.txt"):
        os.utime(path, (T0, T0))
    return root


def service(tmp_path, sftp=None) -> SFTPService:
    backend_config = make_config(tmp_path)
    from conftest import FakeBackend
    return SFTPService(FakeBackend(backend_config, sftp=sftp or FakeSFTP()))


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------
def test_list_dir_reports_type_size_mtime(tmp_path, remote_tree):
    svc = service(tmp_path, remote_tree)
    items = svc.list_dir("/remote")
    assert [(i["name"], i["type"], i["size"]) for i in items] == [
        ("a.txt", "file", 4), ("sub", "dir", 4096)]
    assert items[0]["mtime"] == T0


def test_list_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        service(tmp_path).list_dir("/nope")


# ----------------------------------------------------------------------
# One-shot copies
# ----------------------------------------------------------------------
def test_upload_then_download_roundtrip(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    stats = svc.upload(str(local_tree / "a.txt"), "/remote/up.txt", recursive=False)
    assert stats["copied"] == 1
    assert remote_tree.tree["/remote/up.txt"][0] == b"aaaa"
    assert remote_tree.tree["/remote/up.txt"][1] == T0   # mtime preserved

    stats = svc.download("/remote/a.txt", str(tmp_path / "down.txt"), recursive=False)
    assert (tmp_path / "down.txt").read_text() == "aaaa"
    assert os.path.getmtime(tmp_path / "down.txt") == T0
    assert stats["bytes"] == 4


def test_download_into_existing_directory_keeps_the_name(tmp_path, remote_tree):
    target = tmp_path / "out"
    target.mkdir()
    service(tmp_path, remote_tree).download("/remote/a.txt", str(target), recursive=False)
    assert (target / "a.txt").read_text() == "aaaa"


def test_upload_directory_needs_recursive(tmp_path, local_tree):
    with pytest.raises(ValueError, match="recursive"):
        service(tmp_path).upload(str(local_tree), "/remote/x", recursive=False)


def test_download_directory_needs_recursive(tmp_path, remote_tree):
    with pytest.raises(ValueError, match="recursive"):
        service(tmp_path, remote_tree).download("/remote", str(tmp_path / "x"),
                                                recursive=False)


def test_recursive_copy_walks_subtrees(tmp_path, local_tree, remote_tree):
    stats = service(tmp_path, remote_tree).upload(str(local_tree), "/remote/up",
                                                  recursive=True)
    assert stats["copied"] == 2
    assert set(remote_tree.tree) >= {"/remote/up/a.txt", "/remote/up/sub/b.txt"}


# ----------------------------------------------------------------------
# Sync: direction vocabulary
# ----------------------------------------------------------------------
def test_pull_reads_the_remote_side(tmp_path, remote_tree):
    """The original bug swapped pull's arguments and looked for /remote locally."""
    svc = service(tmp_path, remote_tree)
    into = tmp_path / "pulled"
    into.mkdir()
    stats = svc.sync("pull", source="/remote", target=str(into))
    assert stats["copied"] == 2
    assert (into / "a.txt").read_text() == "aaaa"
    assert (into / "sub" / "b.txt").read_text() == "bb"
    assert os.path.getmtime(into / "a.txt") == T0


def test_pull_is_incremental_on_the_second_run(tmp_path, remote_tree):
    svc = service(tmp_path, remote_tree)
    into = tmp_path / "pulled"
    into.mkdir()
    svc.sync("pull", source="/remote", target=str(into))
    again = svc.sync("pull", source="/remote", target=str(into))
    assert (again["copied"], again["skipped"]) == (0, 2)


def test_push_is_incremental_on_the_second_run(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    again = svc.sync("push", source=str(local_tree), target="/remote/mirror")
    assert (again["copied"], again["skipped"]) == (0, 2)


def test_push_then_pull_agree_on_signatures(tmp_path, local_tree, remote_tree):
    """A file that survives push->pull must not be re-copied on the way back."""
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    into = tmp_path / "back"
    into.mkdir()
    pulled = svc.sync("pull", source="/remote/mirror", target=str(into))
    assert (pulled["copied"], pulled["skipped"]) == (2, 0)
    roundtrip = svc.sync("pull", source="/remote/mirror", target=str(into))
    assert (roundtrip["copied"], roundtrip["skipped"]) == (0, 2)


def test_changed_content_is_recopied(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    into = tmp_path / "pulled"
    into.mkdir()
    svc.sync("pull", source="/remote", target=str(into))
    remote_tree.add_file("/remote/a.txt", "different content", mtime=T0 + 50)
    stats = svc.sync("pull", source="/remote", target=str(into))
    assert stats["copied"] == 1 and stats["skipped"] == 1
    assert (into / "a.txt").read_text() == "different content"


def test_unknown_direction_is_a_value_error(tmp_path, remote_tree):
    with pytest.raises(ValueError, match="direction"):
        service(tmp_path, remote_tree).sync("sideways", "/remote", str(tmp_path))


def test_sync_target_that_is_not_a_directory(tmp_path, remote_tree):
    with pytest.raises(ValueError, match="not a directory"):
        service(tmp_path, remote_tree).sync("pull", "/remote", str(tmp_path / "nope"))


def test_dry_run_counts_without_writing(tmp_path, remote_tree):
    into = tmp_path / "dry"
    into.mkdir()
    stats = service(tmp_path, remote_tree).sync("pull", "/remote", str(into), dry_run=True)
    assert stats["copied"] == 2
    assert list(into.iterdir()) == []


def test_dry_run_creates_no_remote_directories(tmp_path, local_tree, remote_tree):
    stats = service(tmp_path, remote_tree).sync("push", str(local_tree),
                                                "/remote/mirror", dry_run=True)
    assert stats["copied"] == 2
    assert not any(p.startswith("/remote/mirror") for p in remote_tree.tree)


# ----------------------------------------------------------------------
# Mirror deletes
# ----------------------------------------------------------------------
def test_pull_with_delete_prunes_local_only_files(tmp_path, remote_tree):
    into = tmp_path / "pulled"
    (into / "sub").mkdir(parents=True)
    svc = service(tmp_path, remote_tree)
    svc.sync("pull", source="/remote", target=str(into))
    stray = into / "stale.txt"
    stray.write_text("gone upstream")
    stats = svc.sync("pull", source="/remote", target=str(into), delete=True)
    assert stats["deleted"] == 1
    assert not stray.exists()


def test_push_with_delete_prunes_remote_only_files(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    remote_tree.add_file("/remote/mirror/orphan.txt", "not local")
    stats = svc.sync("push", source=str(local_tree), target="/remote/mirror", delete=True)
    assert stats["deleted"] == 1
    assert "/remote/mirror/orphan.txt" not in remote_tree.tree


def test_push_delete_removes_stale_directories(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    remote_tree.add_dir("/remote/mirror/gone")
    remote_tree.add_file("/remote/mirror/gone/deep.txt", "x")
    stats = svc.sync("push", source=str(local_tree), target="/remote/mirror", delete=True)
    assert stats["deleted"] >= 1
    assert not any(p.startswith("/remote/mirror/gone") for p in remote_tree.tree)


def test_delete_reaches_into_subdirectories(tmp_path, local_tree, remote_tree):
    """--delete must not be top-level only: a stale file under sub/ goes too."""
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    remote_tree.add_file("/remote/mirror/sub/stale.txt", "not local")
    stats = svc.sync("push", source=str(local_tree), target="/remote/mirror", delete=True)
    assert stats["deleted"] == 1
    assert "/remote/mirror/sub/stale.txt" not in remote_tree.tree
    assert "/remote/mirror/sub/b.txt" in remote_tree.tree


def test_delete_dry_run_reports_without_removing(tmp_path, local_tree, remote_tree):
    svc = service(tmp_path, remote_tree)
    svc.sync("push", source=str(local_tree), target="/remote/mirror")
    remote_tree.add_file("/remote/mirror/orphan.txt", "x")
    stats = svc.sync("push", source=str(local_tree), target="/remote/mirror",
                     delete=True, dry_run=True)
    assert stats["deleted"] == 1
    assert "/remote/mirror/orphan.txt" in remote_tree.tree


# ----------------------------------------------------------------------
# Servers that refuse utime
# ----------------------------------------------------------------------
def test_missing_utime_support_is_reported_not_fatal(tmp_path, local_tree):
    sftp = FakeSFTP(utime_supported=False)
    sftp.add_dir("/remote")
    svc = service(tmp_path, sftp)
    first = svc.sync("push", source=str(local_tree), target="/remote/mirror")
    assert first["copied"] == 2
    assert first["warnings"] and "mtime not preserved" in first["warnings"][0]
    assert sftp.utime_calls >= 1
    # without preserved mtimes the next run cannot skip: it copies again, loudly
    second = svc.sync("push", source=str(local_tree), target="/remote/mirror")
    assert second["copied"] == 2
    assert second["warnings"]


def test_transfer_errors_are_counted_not_raised(tmp_path, remote_tree):
    """A file that vanishes mid-walk must not abort the whole sync."""
    svc = service(tmp_path, remote_tree)
    original_get = remote_tree.get

    def flaky(remote, local):
        if remote.endswith("a.txt"):
            raise OSError("connection reset")
        original_get(remote, local)

    remote_tree.get = flaky
    into = tmp_path / "p"
    into.mkdir()
    stats = svc.sync("pull", source="/remote", target=str(into))
    assert stats["errors"] == 1
    assert (into / "sub" / "b.txt").read_text() == "bb"


def test_server_info_names_the_target(tmp_path):
    svc = service(tmp_path)
    assert svc.server_info == {"host": "", "port": 22}


def test_concurrent_syncs_are_serialised(tmp_path, remote_tree, local_tree):
    """One SFTP session at a time: two syncs must not interleave on the client."""
    import threading
    svc = service(tmp_path, remote_tree)
    into = tmp_path / "threaded"
    into.mkdir()
    errors = []

    def worker():
        try:
            for _ in range(3):
                svc.sync("pull", source="/remote", target=str(into))
        except Exception as e:  # noqa: BLE001 - the test asserts on it
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert time.time() - started < 30
