"""SFTP service (sftp.py)

Recursive upload / download / mirror-sync / listing on top of
paramiko.SFTPClient. Direction vocabulary, used consistently everywhere:

  - "local"  = the filesystem of the host running the gateway
  - "remote" = the target's filesystem, reached over SFTP

Copy policy: a file is skipped when size and mtime already match on both
sides, so a repeated sync is cheap. mtimes are therefore restored after every
copy (``sf.utime`` / ``os.utime``); if the server rejects ``utime`` the
warning is reported and that run falls back to copying.
"""

from __future__ import annotations

import logging
import os
import posixpath
import shutil
import threading

import paramiko

log = logging.getLogger("ssh_gateway.sftp")

_MTIME_TOLERANCE_S = 2


def _is_dir(attr) -> bool:
    mode = getattr(attr, "st_mode", None)
    return mode is not None and (mode & 0o170000) == 0o040000


def _sig(size, mtime) -> tuple[int, int]:
    return int(size), int(mtime)


def _same(sig_a, sig_b) -> bool:
    return sig_a[0] == sig_b[0] and abs(sig_a[1] - sig_b[1]) <= _MTIME_TOLERANCE_S


def _new_stats(direction: str) -> dict:
    return {"direction": direction, "copied": 0, "skipped": 0, "deleted": 0,
            "created_dirs": 0, "errors": 0, "bytes": 0, "warnings": []}


class SFTPService:
    """File transfers against the injected backend. One SFTP session at a time."""

    def __init__(self, backend):
        self._backend = backend
        self._lock = threading.RLock()

    @property
    def server_info(self) -> dict:
        return {"host": self._backend.cfg.remote_host, "port": self._backend.cfg.remote_port}

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_dir(self, remote: str) -> list[dict]:
        with self._lock, self._backend.sftp() as sf:
            entries = self._list_entries(sf, remote)
            items = [{"name": name,
                      "type": "dir" if _is_dir(attr) else "file",
                      "size": attr.st_size,
                      "mtime": getattr(attr, "st_mtime", None)}
                     for name, attr in entries.items()]
        items.sort(key=lambda x: (x["type"] == "dir", x["name"]))
        return items

    # ------------------------------------------------------------------
    # One-shot copy (upload = local -> remote, download = remote -> local)
    # ------------------------------------------------------------------
    def upload(self, local: str, remote: str, recursive: bool = False) -> dict:
        local = os.path.abspath(local)
        stats = _new_stats("push")
        if os.path.isdir(local) and not recursive:
            raise ValueError("local is a directory: pass recursive=true")
        with self._lock, self._backend.sftp() as sf:
            if os.path.isdir(local):
                self._mkdir_p(sf, remote)
                self._copy_tree_up(sf, local, remote, stats)
            else:
                self._put_file(sf, local, remote, stats)
        return stats

    def download(self, remote: str, local: str, recursive: bool = False) -> dict:
        local = os.path.abspath(local)
        stats = _new_stats("pull")
        with self._lock, self._backend.sftp() as sf:
            attr = self._stat_remote(sf, remote)
            if _is_dir(attr):
                if not recursive:
                    raise ValueError("remote is a directory: pass recursive=true")
                os.makedirs(local, exist_ok=True)
                self._copy_tree_down(sf, remote, local, stats)
            else:
                if os.path.isdir(local) or local.endswith((os.sep, "/")):
                    local = os.path.join(local, posixpath.basename(remote.rstrip("/")))
                self._get_file(sf, remote, local, stats)
        return stats

    # ------------------------------------------------------------------
    # Mirror sync: push = local->remote, pull = remote->local
    # ------------------------------------------------------------------
    def sync(self, direction: str, source: str, target: str,
             delete: bool = False, dry_run: bool = False) -> dict:
        direction = (direction or "push").lower()
        if direction == "push":
            local_dir, remote_dir = os.path.abspath(source), target
        elif direction == "pull":
            local_dir, remote_dir = os.path.abspath(target), source
        else:
            raise ValueError("direction must be 'push' (local->remote) or 'pull' (remote->local)")
        if not os.path.isdir(local_dir):
            raise ValueError(f"local side is not a directory: {local_dir}")

        stats = _new_stats(direction)
        stats["dry_run"] = dry_run
        # Deletes are mirrored per directory, inside the walk, so that a stale
        # file in a subdirectory is pruned too.
        with self._lock, self._backend.sftp() as sf:
            if direction == "push":
                self._sync_up(sf, local_dir, remote_dir, stats, delete, dry_run)
            else:
                self._sync_down(sf, remote_dir, local_dir, stats, delete, dry_run)
        return stats

    # ==================================================================
    # Internals
    # ==================================================================
    @staticmethod
    def _stat_remote(sf, path: str):
        try:
            return sf.stat(path)
        except FileNotFoundError:
            # Re-raised with the path in it; the original carries nothing useful.
            raise FileNotFoundError(f"remote path does not exist: {path}") from None

    @staticmethod
    def _list_entries(sf, path: str) -> dict[str, paramiko.SFTPAttributes]:
        """Entries under a remote directory, or {basename: attr} for a file."""
        attr = SFTPService._stat_remote(sf, path)
        if _is_dir(attr):
            return {a.filename: a for a in sf.listdir_attr(path) if a.filename not in (".", "..")}
        return {posixpath.basename(path.rstrip("/")): attr}

    @staticmethod
    def _safe_list(sf, path: str) -> dict:
        try:
            return SFTPService._list_entries(sf, path)
        except FileNotFoundError:
            return {}

    @staticmethod
    def _mkdir_p(sf, remote: str) -> None:
        parts = [p for p in remote.split("/") if p]
        cur = "/" if remote.startswith("/") else ""
        for p in parts:
            cur = cur.rstrip("/") + "/" + p
            try:
                sf.mkdir(cur, mode=0o755)
            except OSError:
                pass  # already exists

    @staticmethod
    def _join(remote: str, name: str) -> str:
        return remote.rstrip("/") + "/" + name

    # ---- single file ----
    def _put_file(self, sf, local: str, remote: str, stats: dict) -> bool:
        st = os.stat(local)
        try:
            self._mkdir_p(sf, posixpath.dirname(remote))
            sf.put(local, remote)
            self._remote_utime(sf, remote, st.st_mtime, stats)
            stats["copied"] += 1
            stats["bytes"] += st.st_size
            return True
        except Exception as e:
            stats["errors"] += 1
            log.warning("upload failed %s -> %s: %s", local, remote, e)
            return False

    def _get_file(self, sf, remote: str, local: str, stats: dict) -> bool:
        try:
            remote_attr = sf.stat(remote)
            os.makedirs(os.path.dirname(local) or ".", exist_ok=True)
            sf.get(remote, local)
            mtime = getattr(remote_attr, "st_mtime", None)
            if mtime is not None:
                os.utime(local, ns=(int(mtime) * 10**9, int(mtime) * 10**9))
            stats["copied"] += 1
            stats["bytes"] += int(remote_attr.st_size)
            return True
        except Exception as e:
            stats["errors"] += 1
            log.warning("download failed %s -> %s: %s", remote, local, e)
            return False

    @staticmethod
    def _remote_utime(sf, remote: str, mtime: float, stats: dict) -> None:
        """Best-effort mtime restore; a server that refuses it breaks nothing
        except the skip-on-next-run optimisation, which is reported."""
        try:
            sf.utime(remote, (None, int(mtime)))
        except Exception as e:
            msg = f"remote mtime not preserved ({type(e).__name__}): repeat syncs will re-copy"
            if msg not in stats["warnings"]:
                stats["warnings"].append(msg)
            log.debug("utime %s: %s", remote, e)

    # ---- trees ----
    def _copy_tree_up(self, sf, local: str, remote: str, stats: dict) -> None:
        self._mkdir_p(sf, remote)
        for entry in sorted(os.listdir(local)):
            lp, rp = os.path.join(local, entry), self._join(remote, entry)
            if os.path.isdir(lp):
                self._copy_tree_up(sf, lp, rp, stats)
            else:
                self._put_file(sf, lp, rp, stats)

    def _copy_tree_down(self, sf, remote: str, local: str, stats: dict) -> None:
        os.makedirs(local, exist_ok=True)
        for name, attr in sorted(self._safe_list(sf, remote).items()):
            lp, rp = os.path.join(local, name), self._join(remote, name)
            if _is_dir(attr):
                self._copy_tree_down(sf, rp, lp, stats)
            else:
                self._get_file(sf, rp, lp, stats)

    # ---- incremental sync ----
    def _sync_up(self, sf, local: str, remote: str, stats: dict,
                 delete: bool, dry_run: bool) -> None:
        if not dry_run:
            self._mkdir_p(sf, remote)
        remote_children = self._safe_list(sf, remote)
        for entry in sorted(os.listdir(local)):
            lp = os.path.join(local, entry)
            rp = self._join(remote, entry)
            if os.path.isdir(lp):
                self._sync_up(sf, lp, rp, stats, delete, dry_run)
                continue
            st = os.stat(lp)
            peer = remote_children.get(entry)
            if peer is not None and _same(_sig(st.st_size, st.st_mtime),
                                          _sig(peer.st_size, peer.st_mtime or 0)):
                stats["skipped"] += 1
                continue
            if dry_run:
                stats["copied"] += 1
                stats["bytes"] += st.st_size
                continue
            self._put_file(sf, lp, rp, stats)
        if delete:
            self._prune_remote(sf, remote, local, stats, dry_run)

    def _sync_down(self, sf, remote: str, local: str, stats: dict,
                   delete: bool, dry_run: bool) -> None:
        if not dry_run:
            os.makedirs(local, exist_ok=True)
        for name, attr in sorted(self._safe_list(sf, remote).items()):
            lp = os.path.join(local, name)
            rp = self._join(remote, name)
            if _is_dir(attr):
                self._sync_down(sf, rp, lp, stats, delete, dry_run)
                continue
            if os.path.isfile(lp):
                st = os.stat(lp)
                if _same(_sig(st.st_size, st.st_mtime),
                         _sig(attr.st_size, attr.st_mtime or 0)):
                    stats["skipped"] += 1
                    continue
            if dry_run:
                stats["copied"] += 1
                stats["bytes"] += int(attr.st_size)
                continue
            self._get_file(sf, rp, lp, stats)
        if delete:
            self._prune_local(sf, remote, local, stats, dry_run)

    # ---- mirroring deletes ----
    def _prune_remote(self, sf, remote: str, local: str, stats: dict,
                      dry_run: bool = False) -> None:
        """Delete remote entries that do not exist locally."""
        keep = set(os.listdir(local))
        for name, attr in self._safe_list(sf, remote).items():
            if name in keep:
                continue
            rp = self._join(remote, name)
            stats["deleted"] += 1
            if dry_run:
                continue
            try:
                if _is_dir(attr):
                    _rmtree_remote(sf, rp)
                else:
                    sf.remove(rp)
            except Exception as e:
                stats["errors"] += 1
                log.warning("remote delete failed %s: %s", rp, e)

    def _prune_local(self, sf, remote: str, local: str, stats: dict,
                     dry_run: bool = False) -> None:
        """Delete local entries that do not exist remotely."""
        keep = set(self._safe_list(sf, remote).keys())
        if not os.path.isdir(local):
            return
        for name in os.listdir(local):
            if name in keep:
                continue
            lp = os.path.join(local, name)
            stats["deleted"] += 1
            if dry_run:
                continue
            try:
                if os.path.isdir(lp) and not os.path.islink(lp):
                    shutil.rmtree(lp)
                else:
                    os.remove(lp)
            except Exception as e:
                stats["errors"] += 1
                log.warning("local delete failed %s: %s", lp, e)


def _rmtree_remote(sf, remote: str) -> None:
    for name, attr in SFTPService._safe_list(sf, remote).items():
        child = SFTPService._join(remote, name)
        if _is_dir(attr):
            _rmtree_remote(sf, child)
        else:
            sf.remove(child)
    sf.rmdir(remote)
