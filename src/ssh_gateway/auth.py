"""Bearer token auth (auth.py)

With no token configured the gateway allows everything — that is the right
default for a loopback-only listener, where the local user already has shell
access anyway. Set SSHGW_TOKEN and every non-public route then requires
``Authorization: Bearer <token>``, compared in constant time.
"""

from __future__ import annotations

import hmac
from typing import Optional


class TokenAuth:
    def __init__(self, token: str = ""):
        self.token = (token or "").strip()
        self.enabled = bool(self.token)

    def check_header(self, header: Optional[str]) -> tuple[bool, str]:
        """Return (allow, reason_when_denied)."""
        if not self.enabled:
            return True, ""
        if not header:
            return False, "missing Authorization header"
        scheme, _, credential = header.partition(" ")
        if scheme.lower() != "bearer":
            return False, "authorization scheme must be Bearer"
        if not hmac.compare_digest(credential.strip(), self.token):
            return False, "invalid token"
        return True, ""

    def describe(self) -> dict:
        return {"enabled": self.enabled}
