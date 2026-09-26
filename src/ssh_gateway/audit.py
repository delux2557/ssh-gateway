"""Audit log (audit.py)

Every consequential operation (execution, denial, job lifecycle, file
transfer, failed auth) is recorded twice, independently:

  - an in-memory ring buffer, surfaced by GET /audit
  - an append-only JSONL file under the state directory

Writing neither may break the request path, so failures degrade to a warning.
Note the file records command text verbatim — keep the state directory out of
version control and treat it as sensitive.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from typing import Any, Optional

from .models import AuditEntry, now_iso

log = logging.getLogger("ssh_gateway.audit")


class AuditLogger:
    def __init__(self, enabled: bool = True, file: str = "", max_entries: int = 2000):
        self.enabled = enabled
        self.max_entries = max_entries
        self._ring: deque[AuditEntry] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._file = file
        if file:
            os.makedirs(os.path.dirname(file) or ".", exist_ok=True)

    def record(self, action: str, method: str = "?", path: str = "?",
               client: str = "", cmd: Optional[str] = None,
               target: Optional[str] = None, result: str = "ok",
               detail: Any = None) -> Optional[AuditEntry]:
        if not self.enabled:
            return None
        entry = AuditEntry(ts=now_iso(), method=method, path=path,
                           client=client or "", action=action,
                           cmd=cmd, target=target, result=result,
                           detail=_jsonable(detail))
        with self._lock:
            self._ring.append(entry)
            if self._file:
                try:
                    with open(self._file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry.dict(), ensure_ascii=False) + "\n")
                except OSError as e:
                    log.warning("audit file write failed: %s", e)
        return entry

    def recent(self, limit: int = 100, action: Optional[str] = None) -> list[dict]:
        with self._lock:
            items = list(self._ring)
        if action:
            items = [e for e in items if e.action == action]
        return [e.dict() for e in items[-limit:]]

    def count(self) -> int:
        with self._lock:
            return len(self._ring)

    def enabled_flag(self) -> bool:
        return self.enabled


def _jsonable(obj: Any) -> Any:
    """Detail that will not serialize degrades to its repr instead of failing."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    try:
        json.dumps(obj)
        return obj
    except (TypeError, ValueError):
        return str(obj)
