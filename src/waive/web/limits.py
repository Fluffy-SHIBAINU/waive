"""Request body cap and request rate limits (spec §11). FastAPI parses a whole multipart body
before any route code or token check runs, so the body limit sits in front of the app and stops
the bytes at the socket; the rate limiter is consulted by the routes that need no sign-in."""

import time
from collections.abc import Callable

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class TooManyRequests(HTTPException):
    """A fixed window filled up; the app renders a plain "wait a few minutes" page (429)."""

    def __init__(self, what: str) -> None:
        super().__init__(status_code=429, detail=f"too many {what}")


class RateLimiter:
    """Fixed windows kept in process memory: enough for the single replica this app runs as.
    Keys are chosen by the caller: a global ceiling ("cases") that no header can dodge, plus a
    best-effort per-client key (behind the front end the client address is whatever
    X-Forwarded-For says, so the global key is the one that holds)."""

    MAX_KEYS = 10_000

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._offset = 0.0
        self._windows: dict[str, tuple[int, float]] = {}

    def _now(self) -> float:
        return self._clock() + self._offset

    def advance(self, seconds: float) -> None:
        """Move the limiter's clock forward (tests; the windows expire as time passes)."""
        self._offset += seconds

    def _live(self, key: str, seconds: float) -> tuple[int, float]:
        now = self._now()
        count, started = self._windows.get(key, (0, now))
        if now - started >= seconds:
            return 0, now
        return count, started

    def count(self, key: str, seconds: float) -> int:
        return self._live(key, seconds)[0]

    def exceeded(self, key: str, limit: int, seconds: float) -> bool:
        return self.count(key, seconds) >= limit

    def hit(self, key: str, limit: int, seconds: float) -> bool:
        """Record one event under `key`; True while the window holds at most `limit` events."""
        count, started = self._live(key, seconds)
        if len(self._windows) >= self.MAX_KEYS:
            now = self._now()
            self._windows = {k: (c, s) for k, (c, s) in self._windows.items() if now - s < seconds}
        self._windows[key] = (count + 1, started)
        return count + 1 <= limit

    def check(self, key: str, limit: int, seconds: float, what: str) -> None:
        if not self.hit(key, limit, seconds):
            raise TooManyRequests(what)

    def reset(self, key: str) -> None:
        self._windows.pop(key, None)


def client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


class UploadTooLarge(HTTPException):
    """The request body is over WAIVE_MAX_UPLOAD_BYTES. An HTTPException so FastAPI's body
    parsing re-raises it unchanged and the app's handler can render a plain page."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=f"request body over {limit} bytes")


class BodyLimit:
    """Pure-ASGI wrapper: rejects a declared Content-Length over the cap, and counts the bytes of
    a chunked body as they arrive. The error is raised from `receive`, inside the router, so the
    app's exception handlers (not a bare 500) answer it."""

    def __init__(self, app: ASGIApp, max_body: int) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        seen = 0

        async def capped() -> Message:
            nonlocal seen
            if declared.isdigit() and int(declared) > self.max_body:
                raise UploadTooLarge(self.max_body)
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.max_body:
                    raise UploadTooLarge(self.max_body)
            return message

        await self.app(scope, capped, send)
