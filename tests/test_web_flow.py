from pathlib import Path

import aiosqlite
import httpx
import pytest

from bot_app.config import Settings
from bot_app.db import init_db
from bot_app.web_app import create_web_app


class FakeOsuClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, endpoint: str):
        self.calls.append(endpoint)
        if endpoint == "users/testuser":
            return {
                "id": 24680,
                "username": "testuser",
                "playmode": "osu",
                "statistics": {"global_rank": 12345},
                "page": {"raw": ""},
            }
        if endpoint == "users/24680":
            return {
                "id": 24680,
                "username": "testuser",
                "playmode": "osu",
                "statistics": {"global_rank": 12345},
                "page": {"raw": "verify-token here"},
            }
        return None


@pytest.mark.asyncio
async def test_start_and_finalize_verification(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    await init_db(str(db_path))
    settings = Settings(
        discord_bot_token="x",
        discord_guild_id=1,
        discord_owner_id=None,
        osu_client_id="id",
        osu_client_secret="secret",
        webhook_secret="secret",
        base_url="http://localhost",
        database_path=str(db_path),
        verification_mode="rank_digit_count",
        digit_modulus=10,
        verification_token_ttl_seconds=900,
        link_code_ttl_seconds=900,
        rate_limit_per_minute=30,
        osu_cache_ttl_seconds=30,
        role_mapping_path="unused",
        osu_redirect_uri="http://localhost/auth/osu/callback",
        cors_origins="",
    )
    role_mapping = {"osu": {5: 999}}
    osu_client = FakeOsuClient()
    app = create_web_app(settings=settings, osu_client=osu_client, role_mapping=role_mapping)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        start = await client.post(
            "/verify/start",
            json={"discord_id": 101, "osu_identifier": "testuser"},
        )
        assert start.status_code == 200
        payload = start.json()
        challenge_id = payload["challenge_id"]

        async with aiosqlite.connect(str(db_path)) as db:
            await db.execute(
                "INSERT INTO verified_discord_links (discord_id, verified_at) VALUES (?, ?)",
                (101, 1),
            )
            await db.execute(
                "UPDATE verification_challenges SET profile_token=? WHERE id=?",
                ("verify-token", challenge_id),
            )
            await db.commit()

        finalize = await client.post("/verify/finalize", json={"challenge_id": challenge_id})
        assert finalize.status_code == 200
        assert finalize.json()["role_id"] == 999
