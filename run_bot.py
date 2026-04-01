"""Run Discord bot process."""

import asyncio

from bot_app.main_bot import run_bot

if __name__ == "__main__":
    asyncio.run(run_bot())
