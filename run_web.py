"""Run FastAPI app process."""

from __future__ import annotations

import asyncio

import uvicorn

from bot_app.main_web import build_web_app


class LazyASGIApp:
    """ASGI wrapper that initializes FastAPI app inside event loop."""

    def __init__(self) -> None:
        self._app = None
        self._lock = asyncio.Lock()

    async def _get_app(self):
        if self._app is None:
            async with self._lock:
                if self._app is None:
                    self._app = await build_web_app()
        return self._app

    async def __call__(self, scope, receive, send):
        app = await self._get_app()
        await app(scope, receive, send)


app = LazyASGIApp()


def main() -> None:
    uvicorn.run("run_web:app", host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()