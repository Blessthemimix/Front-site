"""osu! OAuth2 authorization-code helpers (user login as application)."""

from __future__ import annotations

from urllib.parse import urlencode

import httpx

OSU_AUTHORIZE = "https://osu.ppy.sh/oauth/authorize"
OSU_TOKEN = "https://osu.ppy.sh/oauth/token"
OSU_ME = "https://osu.ppy.sh/api/v2/me"


def build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    scope: str = "public",
) -> str:
    """Build osu! OAuth authorize URL (user is redirected here)."""
    q = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
        }
    )
    return f"{OSU_AUTHORIZE}?{q}"


async def exchange_authorization_code(client_id: str, client_secret: str, code: str, redirect_uri: str, http: httpx.AsyncClient | None = None) -> dict:
    """Exchange OAuth code for tokens (server-side)."""
    own = http is None
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.post(
            OSU_TOKEN,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
        response.raise_for_status()
        return response.json()
    finally:
        if own:
            await client.aclose()


async def fetch_me(access_token: str, http: httpx.AsyncClient | None = None) -> dict | None:
    """GET /api/v2/me with the user's access token."""
    own = http is None
    client = http or httpx.AsyncClient(timeout=20.0)
    try:
        response = await client.get(
            OSU_ME,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code != 200:
            return None
        return response.json()
    finally:
        if own:
            await client.aclose()
