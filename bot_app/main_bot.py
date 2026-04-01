"""Bot process entrypoint helpers."""

from __future__ import annotations

from .config import load_role_mapping, load_settings
from .db import init_db
from .discord_client import RoleBot, register_commands
from .logging_utils import setup_logging
from .osu_client import OsuClient


async def run_bot() -> None:
    """Initialize and run Discord bot."""
    setup_logging()
    settings = load_settings()
    await init_db(settings.database_path)
    role_mapping = load_role_mapping(settings.role_mapping_path)
    osu_client = OsuClient(
        settings.osu_client_id, settings.osu_client_secret, cache_ttl=settings.osu_cache_ttl_seconds
    )
    bot = RoleBot(settings=settings, osu_client=osu_client, role_mapping=role_mapping)
    register_commands(bot)
    await bot.start(settings.discord_bot_token)
