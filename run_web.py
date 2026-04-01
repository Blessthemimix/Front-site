"""Run FastAPI app process."""

from __future__ import annotations

import uvicorn

from bot_app.main_web import create_app

app = create_app()


def main() -> None:
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()