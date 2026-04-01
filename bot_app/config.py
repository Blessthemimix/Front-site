"""Application configuration and environment loading."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass
class Settings:
    discord_bot_token: str
    discord_guild_id: int
    discord_owner_id: int
    osu_client_id: int
    osu_client_secret: str
    webhook_secret: str
    base_url: str
    database_path: str
    verification_mode: str
    digit_modulus: int
    verification_token_ttl_seconds: int
    link_code_ttl_seconds: int
    rate_limit_per_minute: int
    osu_cache_ttl_seconds: int
    role_mapping_path: str


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def load_settings(*, require_discord: bool = True, require_osu: bool = True) -> Settings:
    """Load settings from environment variables."""
    load_dotenv()
    discord_bot_token = _required("DISCORD_BOT_TOKEN") if require_discord else os.getenv("DISCORD_BOT_TOKEN", "")
    discord_guild_raw = _required("DISCORD_GUILD_ID") if require_discord else os.getenv("DISCORD_GUILD_ID", "0")
    osu_client_id = _required("OSU_CLIENT_ID") if require_osu else os.getenv("OSU_CLIENT_ID", "")
    osu_client_secret = _required("OSU_CLIENT_SECRET") if require_osu else os.getenv("OSU_CLIENT_SECRET", "")
    return Settings(
        discord_bot_token=discord_bot_token,
        discord_guild_id=int(discord_guild_raw),
        discord_owner_id=int(os.getenv("DISCORD_OWNER_ID", "0")) or None,
        osu_client_id=osu_client_id,
        osu_client_secret=osu_client_secret,
        webhook_secret=_required("WEBHOOK_SECRET"),
        base_url=os.getenv("BASE_URL", "http://localhost:8000"),
        database_path=os.getenv("DATABASE_PATH", "./data/bot_data.db"),
        verification_mode=os.getenv("VERIFICATION_MODE", "rank_digit_count"),
        digit_modulus=int(os.getenv("DIGIT_MODULUS", "10")),
        verification_token_ttl_seconds=int(os.getenv("VERIFICATION_TOKEN_TTL_SECONDS", "900")),
        link_code_ttl_seconds=int(os.getenv("LINK_CODE_TTL_SECONDS", "900")),
        rate_limit_per_minute=int(os.getenv("RATE_LIMIT_PER_MINUTE", "15")),
        osu_cache_ttl_seconds=int(os.getenv("OSU_CACHE_TTL_SECONDS", "30")),
        role_mapping_path=os.getenv("ROLE_MAPPING_PATH", "./config/role_mapping.json"),
    )


def load_role_mapping(path: str) -> dict[str, dict[int, int]]:
    """Load role mapping from JSON file."""
    file_path = Path(path)
    if not file_path.exists():
        example = file_path.with_name("role_mapping.example.json")
        raise FileNotFoundError(f"Role mapping not found: {file_path}. Copy from {example}.")
    raw: dict[str, dict[str, Any]] = json.loads(file_path.read_text(encoding="utf-8"))
    mapping: dict[str, dict[int, int]] = {}
    for mode, value in raw.items():
        mapping[mode] = {int(k): int(v) for k, v in value.items()}
    return mapping
