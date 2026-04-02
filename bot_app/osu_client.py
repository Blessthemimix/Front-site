"""Async osu! API client with token management and TTL cache."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class _CacheEntry:
    value: dict[str, Any]
    expires_at: float


class OsuClient:
    """Client credentials osu! API v2 wrapper."""

    def __init__(self, client_id: str, client_secret: str, cache_ttl: int = 30) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_ttl = cache_ttl
        self._token: str | None = None
        self._token_expiry = 0.0
        self._cache: dict[str, _CacheEntry] = {}
        # (beatmap_id, ruleset_id, mods_json) -> star rating at expiry
        self._star_rating_cache: dict[str, tuple[float, float]] = {}
        self._http = httpx.AsyncClient(verify=False)

    async def close(self) -> None:
        await self._http.aclose()

    async def _ensure_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry:
            return self._token
        response = await self._http.post(
            "https://osu.ppy.sh/oauth/token",
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "client_credentials",
                "scope": "public",
            },
        )
        response.raise_for_status()
        data = response.json()
        self._token = data["access_token"]
        self._token_expiry = now + int(data["expires_in"]) - 60
        return self._token

    async def request(self, endpoint: str) -> dict[str, Any] | None:
        """Perform authenticated osu! API call."""
        now = time.time()
        cached = self._cache.get(endpoint)
        if cached and now < cached.expires_at:
            return cached.value
        token = await self._ensure_token()
        response = await self._http.get(
            f"https://osu.ppy.sh/api/v2/{endpoint}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if response.status_code != 200:
            return None
        payload = response.json()
        self._cache[endpoint] = _CacheEntry(value=payload, expires_at=now + self.cache_ttl)
        return payload

    async def beatmap_star_rating(
        self,
        beatmap_id: int,
        mods: list[dict[str, Any]] | None,
        ruleset_id: int,
        *,
        cache_ttl: int = 3600,
    ) -> float | None:
        """
        Star rating for the given beatmap with mods (matches in-game / top play).
        GET beatmap.difficulty_rating is nomod-only; use this for HT/DT/etc.
        """
        mods_list = mods or []
        mods_key = json.dumps(mods_list, sort_keys=True)
        cache_key = f"{beatmap_id}:{ruleset_id}:{mods_key}"
        now = time.time()
        cached = self._star_rating_cache.get(cache_key)
        if cached and now < cached[1]:
            return cached[0]

        token = await self._ensure_token()
        response = await self._http.post(
            f"https://osu.ppy.sh/api/v2/beatmaps/{beatmap_id}/attributes",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"mods": mods_list, "ruleset_id": ruleset_id},
        )
        if response.status_code != 200:
            return None
        data = response.json()
        attrs = data.get("attributes") if isinstance(data, dict) else None
        if not isinstance(attrs, dict):
            return None
        raw = attrs.get("star_rating")
        if raw is None:
            raw = attrs.get("stars")
        if raw is None:
            return None
        try:
            star = float(raw)
        except (TypeError, ValueError):
            return None
        self._star_rating_cache[cache_key] = (star, now + cache_ttl)
        return star
