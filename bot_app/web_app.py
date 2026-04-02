"""FastAPI app providing osu profile verification flow using PostgreSQL (Supabase)."""

from __future__ import annotations
import logging
import secrets
import time
from typing import Any

import asyncpg # ЗАМЕНЕНО
from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .config import Settings
from .osu_client import OsuClient
from .osu_oauth import build_authorize_url, exchange_authorization_code, fetch_me
from .rate_limiter import RateLimiter
from .verification import VerificationInput, compute_digit_value, extract_osu_identifier
from .db import get_db_conn # ИМПОРТИРУЕМ ТВОЮ НОВУЮ ФУНКЦИЮ

logger = logging.getLogger(__name__)

OAUTH_PROFILE_PLACEHOLDER = "oauth"

class StartVerificationRequest(BaseModel):
    discord_id: int = Field(gt=0)
    osu_identifier: str

class FinalizeVerificationRequest(BaseModel):
    challenge_id: int = Field(gt=0)

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
        conn = await get_db_conn() # ЗАМЕНЕНО
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
            
            # В asyncpg к полям можно обращаться как в словаре
            discord_id = row['discord_id']
            osu_id = row['osu_id']
            osu_username = row['osu_username']
            mode = row['mode']
            profile_token = row['profile_token']
            expires_at = row['expires_at']
            verification_source = row['verification_source']
            status = row['status']

            if status == "completed":
                raise HTTPException(status_code=400, detail="Верификация уже завершена.")
            if now > expires_at:
                raise HTTPException(status_code=410, detail="Срок действия сессии истек.")
            
            # В коде выше при вставке для OAuth мы ставим 0, если Discord еще не привязан
            if not discord_id or discord_id == 0:
                raise HTTPException(
                    status_code=403, 
                    detail="Discord не привязан. Сначала введите команду /linkcode в Discord."
                )

            full_user = await osu_client.request(f"users/{osu_id}")
            if not full_user:
                raise HTTPException(status_code=502, detail="Ошибка API osu!")

            if verification_source != "oauth":
                raw_bio = (full_user.get("page") or {}).get("raw") or ""
                if profile_token not in raw_bio:
                    raise HTTPException(status_code=400, detail="Токен не найден в About Me.")

            stats = full_user.get("statistics") or {}
            vinput = VerificationInput(
                osu_id=int(osu_id),
                username=str(osu_username),
                global_rank=stats.get("global_rank"),
            )
            digit = compute_digit_value(vinput, settings.verification_mode, digit_modulus=settings.digit_modulus)
            role_id = role_mapping.get(mode, {}).get(digit)

            if not role_id:
                raise HTTPException(status_code=400, detail=f"Роль для DIGIT {digit} не настроена.")

            # Сохраняем результат
            async with conn.transaction(): # Транзакция для надежности
                await conn.execute(
                    """INSERT INTO pending_role_assignments 
                    (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)""",
                    discord_id, osu_id, osu_username, mode, digit, role_id, now,
                )
                await conn.execute(
                    """INSERT INTO osu_claims (osu_id, discord_id, claimed_at) VALUES ($1, $2, $3)
                    ON CONFLICT(osu_id) DO UPDATE SET discord_id=EXCLUDED.discord_id, claimed_at=EXCLUDED.claimed_at""",
                    osu_id, discord_id, now,
                )
                await conn.execute("UPDATE verification_challenges SET status='completed' WHERE id=$1", challenge_id)

            return {"ok": True, "role_id": role_id, "digit": digit, "mode": mode}
        finally:
            await conn.close()

    # --- РОУТЫ ---

    @app.get("/", response_class=HTMLResponse)
    async def index(linkcode: str | None = Query(default=None)):
        # Обрабатываем linkcode вместо discord_id
        code_display = linkcode if linkcode else "Код не указан"
        
        html_content = f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Верификация | Discord & osu!</title>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;600&display=swap" rel="stylesheet">
            <style>
                body {{
                    margin: 0; padding: 0;
                    display: flex; justify-content: center; align-items: center;
                    min-height: 100vh;
                    background-color: #121212;
                    color: #ffffff; font-family: 'Inter', sans-serif;
                }}
                .container {{
                    background: #1e1e1e;
                    padding: 40px 50px; border-radius: 12px;
                    border: 1px solid #333;
                    text-align: center;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.6);
                    max-width: 400px; width: 100%;
                }}
                h1 {{ margin-top: 0; font-size: 24px; font-weight: 600; letter-spacing: 0.5px; }}
                p {{ color: #a0a0a0; line-height: 1.6; font-size: 15px; margin-bottom: 20px; }}
                .code-box {{
                    margin: 25px 0; padding: 15px;
                    background: #0a0a0a;
                    border: 1px solid #444; border-radius: 8px;
                    font-size: 22px; font-weight: bold; letter-spacing: 3px;
                    color: #ffffff;
                }}
                .btn {{
                    display: inline-block; width: 100%;
                    margin-top: 15px; padding: 14px 0;
                    background: #ffffff; color: #000000;
                    text-decoration: none; border-radius: 6px;
                    font-weight: 600; font-size: 16px;
                    transition: 0.2s; box-sizing: border-box;
                }}
                .btn:hover {{ background: #d0d0d0; transform: translateY(-2px); }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1>Подтверждение</h1>
                <p>Вы перешли по ссылке для привязки вашего аккаунта osu! к серверу Discord.</p>
                
                <div class="code-box">{code_display}</div>
                
                <p>Если ваш код (<b>/linkcode</b>) совпадает, нажмите кнопку ниже для безопасной авторизации.</p>
                
                <a href="/login/osu" class="btn">Авторизоваться</a>
            </div>
        </body>
        </html>
        """
        return html_content

    @app.get("/auth/osu/login")
    async def osu_oauth_login(request: Request, discord_id: int = Query(..., gt=0)):
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
    async def osu_oauth_callback(request: Request, code: str | None = None, state: str | None = None) -> HTMLResponse:
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow("SELECT discord_id FROM oauth_osu_states WHERE state=$1", state)
            if not row:
                return HTMLResponse("Invalid state", status_code=400)
            
            # Сразу удаляем state
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
            
            # В PostgreSQL используем RETURNING id, чтобы получить ID новой записи
            challenge_id = await conn.fetchval(
                """INSERT INTO verification_challenges 
                (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                VALUES (0, $1, $2, $3, $4, 'pending', $5, $6, 'oauth', $7)
                RETURNING id""",
                osu_id, username, mode, OAUTH_PROFILE_PLACEHOLDER, now, 
                now + settings.verification_token_ttl_seconds, discord_link_code
            )

            return HTMLResponse(content=f"""...твой HTML Шаг 2...""".format(username=username, discord_link_code=discord_link_code, challenge_id=challenge_id))
        finally:
            await conn.close()

    @app.post("/verify/form/finalize", response_class=HTMLResponse)
    async def finalize_from_form(challenge_id: int = Form(...)):
        try:
            result = await _finalize_challenge(challenge_id)
            return HTMLResponse(content=f"""...твой HTML Успех...""".format(result=result))
        except HTTPException as exc:
            return HTMLResponse(content=f"<h2>Ошибка</h2><p>{exc.detail}</p>", status_code=exc.status_code)

    return app