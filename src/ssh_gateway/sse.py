"""Server-Sent Events framing (sse.py)

Events used by the gateway:
  - stdout / stderr : {"text": "..."}
  - exit            : {"exit_code": N, "duration_ms": M}   (or a job's status)
  - error           : {"error": "...", "code": "..."}
"""

from __future__ import annotations

import json


def sse_event(event: str, data) -> str:
    """One frame. JSON keeps embedded newlines escaped, and a raw multi-line
    string is split across data: lines so the framing stays valid either way."""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    lines = "\n".join(f"data: {line}" for line in payload.split("\n"))
    return f"event: {event}\n{lines}\n\n"


def sse_headers() -> dict:
    return {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        # Each stream ends its connection: the client should see EOF once the
        # exit/error frame is written rather than wait on a kept-alive socket.
        "Connection": "close",
        "X-Accel-Buffering": "no",  # tell nginx/caddy not to buffer us
    }
