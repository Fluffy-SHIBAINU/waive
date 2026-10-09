"""Response headers for pages that carry personal data (spec §11).

Everything under a capability link (/s/, /c/), the page that shows freshly minted links (/cases)
and the admin console must not land in a browser or proxy cache, must not be indexed, and must
not hand the link to another site as a Referer. The home page and the public atlas are left alone:
they are meant to be cached and found."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

PRIVATE_PREFIXES = ("/s/", "/c/", "/cases", "/admin")
PRIVATE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-robots-tag", b"noindex, nofollow"),
)


class PrivateHeaders:
    """Pure-ASGI wrapper: sets the headers on every response under PRIVATE_PREFIXES, error pages
    from the exception handlers included."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(PRIVATE_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                names = {name for name, _ in PRIVATE_HEADERS}
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in names
                ]
                message = {**message, "headers": [*headers, *PRIVATE_HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)
