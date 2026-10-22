"""The Kindred ID wall as Waive runs it: open for reading, walled for accounts and the console.

`kindred_auth.KindredWallMiddleware` is the family's verifier, copied verbatim, and it matches its
exempt entries as prefixes. Two Waive rules sit on top of it here, so the copy stays untouched:

- an exempt entry ``/`` opens the home page alone; as a prefix it would open every path;
- on an open path a valid session still names the reader (``request.state.kindred_user``), so the
  home page lists a signed-in reader's cases and a case page offers to keep the case, while a
  request without a session is served all the same and never sent to the Issuer.
"""

from collections.abc import Iterable
from typing import Any

from anyio import to_thread
from starlette.datastructures import Headers

from waive.web import kindred_auth
from waive.web.kindred_auth import KindredWallMiddleware

ROOT = "/"


class Wall(KindredWallMiddleware):
    def __init__(self, app: Any, exempt_prefixes: Iterable[str] = (), **options: Any) -> None:
        prefixes = tuple(exempt_prefixes)
        super().__init__(app, tuple(p for p in prefixes if p != ROOT), **options)
        self.root_open = ROOT in prefixes

    def is_open(self, path: str) -> bool:
        """Served without a session: the home page when "/" is listed, else a listed prefix."""
        if path == ROOT:
            return self.root_open
        return any(path.startswith(prefix) for prefix in self.exempt)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not self.is_open(scope["path"]):
            await super().__call__(scope, receive, send)
            return
        headers = Headers(scope=scope)
        user = kindred_auth.bypass_user(self.env, headers.get("host"))
        token = kindred_auth.read_cookie(headers.get("cookie"))
        if user is None and token:
            # Verification may fetch the key set (blocking urllib); keep it off the event loop.
            user = await to_thread.run_sync(self.verify, token)
        if user is not None:
            scope.setdefault("state", {})["kindred_user"] = user
        await self.app(scope, receive, send)
