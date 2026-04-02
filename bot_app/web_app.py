"""FastAPI app providing osu profile verification flow using PostgreSQL (Supabase)."""

from __future__ import annotations
import logging
import secrets
import time
from typing import Any

import asyncpg
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Settings
from .osu_client import OsuClient
from .osu_oauth import build_authorize_url, exchange_authorization_code, fetch_me
from .rate_limiter import RateLimiter
from .verification import VerificationInput, compute_digit_value
from .db import get_db_conn

logger = logging.getLogger(__name__)

OAUTH_PROFILE_PLACEHOLDER = "oauth"

def create_web_app(
    *,
    settings: Settings,
    osu_client: OsuClient,
    role_mapping: dict[str, dict[int, int]],
) -> FastAPI:
    app = FastAPI(title="osu verification app", version="0.1.0")
    
    if settings.cors_origins:
        origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
    
    limiter = RateLimiter(max_per_minute=settings.rate_limit_per_minute)

    # --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ФИНАЛИЗАЦИИ ---
    async def _finalize_challenge(challenge_id: int) -> dict[str, Any]:
        now = int(time.time())
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow(
                """
                SELECT discord_id, osu_id, osu_username, mode, profile_token, expires_at,
                       COALESCE(verification_source, 'bio') as verification_source, status
                FROM verification_challenges WHERE id=$1
                """,
                challenge_id,
            )
            
            if not row:
                raise HTTPException(status_code=404, detail="Сессия не найдена.")
            
            if row['status'] == "completed":
                raise HTTPException(status_code=400, detail="Верификация уже завершена.")
            if now > row['expires_at']:
                raise HTTPException(status_code=410, detail="Срок действия сессии истек.")
            
            if not row['discord_id'] or row['discord_id'] == 0:
                raise HTTPException(
                    status_code=403, 
                    detail="Discord не привязан. Сначала введите команду /linkcode в Discord."
                )

            full_user = await osu_client.request(f"users/{row['osu_id']}")
            if not full_user:
                raise HTTPException(status_code=502, detail="Ошибка API osu!")

            if row['verification_source'] != "oauth":
                raw_bio = (full_user.get("page") or {}).get("raw") or ""
                if row['profile_token'] not in raw_bio:
                    raise HTTPException(status_code=400, detail="Токен не найден в About Me.")

            stats = full_user.get("statistics") or {}
            vinput = VerificationInput(
                osu_id=int(row['osu_id']),
                username=str(row['osu_username']),
                global_rank=stats.get("global_rank"),
            )
            digit = compute_digit_value(vinput, settings.verification_mode, digit_modulus=settings.digit_modulus)
            role_id = role_mapping.get(row['mode'], {}).get(digit)

            if not role_id:
                raise HTTPException(status_code=400, detail=f"Роль для DIGIT {digit} не настроена.")

            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO pending_role_assignments 
                    (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)""",
                    row['discord_id'], row['osu_id'], row['osu_username'], row['mode'], digit, role_id, now,
                )
                await conn.execute(
                    """INSERT INTO osu_claims (osu_id, discord_id, claimed_at) VALUES ($1, $2, $3)
                    ON CONFLICT(osu_id) DO UPDATE SET discord_id=EXCLUDED.discord_id, claimed_at=EXCLUDED.claimed_at""",
                    row['osu_id'], row['discord_id'], now,
                )
                await conn.execute("UPDATE verification_challenges SET status='completed' WHERE id=$1", challenge_id)

            return {"ok": True, "role_id": role_id, "digit": digit, "mode": row['mode']}
        finally:
            await conn.close()

    # --- РОУТЫ (Все внутри create_web_app) ---

    @app.get("/", response_class=HTMLResponse)
    async def index(linkcode: str | None = Query(default=None)):
        code_display = linkcode if linkcode else "ОЖИДАНИЕ КОДА..."
        auth_url = f"/auth/osu/login?discord_id=0" # Упрощено для примера
        
        return f"""
        <html>
        <body style="background:#121212; color:white; text-align:center; font-family:sans-serif;">
            <div style="margin-top:100px; padding:20px; border:1px solid #333; display:inline-block; border-radius:10px;">
                <h1>osu! Verification</h1>
                <p>Ваш код: <strong>{code_display}</strong></p>
                <a href="{auth_url}" style="color:cyan;">Начать авторизацию</a>
            </div>
        </body>
        </html>
        """

    @app.get("/auth/osu/login")
    async def osu_oauth_login(discord_id: int = Query(..., gt=0)):
        state = secrets.token_urlsafe(32)
        conn = await get_db_conn()
        try:
            await conn.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES ($1, $2, $3, $4)",
                state, discord_id, int(time.time()), int(time.time()) + 600,
            )
        finally:
            await conn.close()
        
        url = build_authorize_url(
            client_id=str(settings.osu_client_id),
            redirect_uri=settings.osu_redirect_uri,
            state=state,
            scope="public",
        )
        return RedirectResponse(url=url)

    @app.get("/auth/osu/callback")
    async def osu_oauth_callback(code: str | None = None, state: str | None = None) -> HTMLResponse:
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow("SELECT discord_id FROM oauth_osu_states WHERE state=$1", state)
            if not row:
                return HTMLResponse("Invalid state", status_code=400)
            
            await conn.execute("DELETE FROM oauth_osu_states WHERE state=$1", state)
            
            tokens = await exchange_authorization_code(
                client_id=str(settings.osu_client_id), 
                client_secret=str(settings.osu_client_secret), 
                code=code, 
                redirect_uri=settings.osu_redirect_uri
            )
            
            me = await fetch_me(tokens["access_token"])
            osu_id, username, mode = int(me["id"]), me["username"], me.get("playmode", "osu")
            
            discord_link_code = secrets.token_hex(3).upper() 
            now = int(time.time())
            
            challenge_id = await conn.fetchval(
                """INSERT INTO verification_challenges 
                (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                VALUES (0, $1, $2, $3, $4, 'pending', $5, $6, 'oauth', $7)
                RETURNING id""",
                osu_id, username, mode, OAUTH_PROFILE_PLACEHOLDER, now, 
                now + settings.verification_token_ttl_seconds, discord_link_code
            )

            return HTMLResponse(content=f"""
                <h2>Привет, {username}!</h2>
                <p>Твой код для Discord: <b>{discord_link_code}</b></p>
                <form action="/verify/form/finalize" method="post">
                    <input type="hidden" name="challenge_id" value="{challenge_id}">
                    <button type="submit">Завершить верификацию</button>
                </form>
            """)
        finally:
            await conn.close()

    @app.post("/verify/form/finalize", response_class=HTMLResponse)
    async def finalize_from_form(challenge_id: int = Form(...)):
        try:
            result = await _finalize_challenge(challenge_id)
            return HTMLResponse(content=f"<h2>Успех!</h2><p>Роль выдана. DIGIT: {result['digit']}</p>")
        except HTTPException as exc:
            return HTMLResponse(content=f"<h2>Ошибка</h2><p>{exc.detail}</p>", status_code=exc.status_code)

    return app