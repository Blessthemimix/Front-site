"""Async osu! API client with token management and TTL cache."""

from __future__ import annotations

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
        self._http = httpx.AsyncClient(timeout=15.0)

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
