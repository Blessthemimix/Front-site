import httpx
import pytest

from bot_app.osu_client import OsuClient


@pytest.mark.asyncio
async def test_osu_client_caches_response() -> None:
    calls = {"token": 0, "user": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600})
        if request.url.path.endswith("/api/v2/users/test"):
            calls["user"] += 1
            return httpx.Response(200, json={"id": 1, "username": "test"})
        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(handler)
    client = OsuClient("id", "secret", cache_ttl=60)
    client._http = httpx.AsyncClient(transport=transport)  # noqa: SLF001
    try:
        a = await client.request("users/test")
        b = await client.request("users/test")
    finally:
        await client.close()

    assert a == {"id": 1, "username": "test"}
    assert b == {"id": 1, "username": "test"}
    assert calls["token"] == 1
    assert calls["user"] == 1
