"""Run FastAPI app process."""

import asyncio

import uvicorn

from bot_app.main_web import build_web_app


def main() -> None:
    app = asyncio.run(build_web_app())
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
