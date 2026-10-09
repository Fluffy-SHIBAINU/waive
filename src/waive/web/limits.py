"""Request body cap (spec §11). FastAPI parses a whole multipart body before any route code or
token check runs, so the limit sits in front of the app and stops the bytes at the socket."""

from fastapi import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
