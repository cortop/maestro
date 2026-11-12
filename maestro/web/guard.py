"""Write guard for `maestro web`: the one gate every state-changing (POST) route goes through."""
from __future__ import annotations

import hmac
import ipaddress
import secrets
from typing import Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, HTTPException, Request

TOKEN_HEADER = "X-Maestro-Token"


def is_loopback(host: str) -> bool:
    """True for `localhost` and loopback IP literals; any other bind address is remote."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class WriteGuard:
    """Per-process token, `Origin` check, read-only mode and the remote-bind refusal."""

    def __init__(self, host: str = "127.0.0.1", *, read_only: bool = False,
                 allow_remote_writes: bool = False):
        self.host = host
        self.read_only = read_only
        self.allow_remote_writes = allow_remote_writes
        self.token = secrets.token_urlsafe(32)

    @property
    def writes_enabled(self) -> bool:
        """Whether any state-changing route may run; templates hide action buttons when False."""
        if self.read_only:
            return False
        return is_loopback(self.host) or self.allow_remote_writes

    async def check(self, request: Request) -> None:
        """FastAPI dependency: raise 403 unless this request may change state."""
        if not self.writes_enabled:
            raise HTTPException(403, "writes are disabled")
        supplied = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(supplied.encode(), self.token.encode()):
            raise HTTPException(403, "missing or invalid token")
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
            raise HTTPException(403, "foreign origin")

    def post(self, app: FastAPI | APIRouter, path: str, handler: Callable, **kw) -> None:
        """Mount *handler* as a guarded POST route; nothing is mounted in read-only mode."""
        if self.read_only:
            return
        from fastapi import Depends
        app.add_api_route(path, handler, methods=["POST"], dependencies=[Depends(self.check)], **kw)
