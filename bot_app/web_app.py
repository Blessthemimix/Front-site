"""FastAPI app providing osu profile verification flow using PostgreSQL (Supabase)."""

from __future__ import annotations
import logging
import secrets
import time
from typing import Any

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

    # --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ФИНАЛИЗАЦИИ ---
    async def _finalize_challenge(challenge_id: int) -> dict[str, Any]:
        now = int(time.time())
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow(
                "SELECT * FROM verification_challenges WHERE id=$1", challenge_id
            )
            if not row:
                raise HTTPException(status_code=404, detail="Сессия не найдена.")
            if row['status'] == "completed":
                raise HTTPException(status_code=400, detail="Верификация уже завершена.")
            if now > row['expires_at']:
                raise HTTPException(status_code=410, detail="Срок действия сессии истек.")
            
            full_user = await osu_client.request(f"users/{row['osu_id']}")
            if not full_user:
                raise HTTPException(status_code=502, detail="Ошибка API osu!")

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
                await conn.execute("UPDATE verification_challenges SET status='completed' WHERE id=$1", challenge_id)

            return {"ok": True, "digit": digit}
        finally:
            await conn.close()

    # --- РОУТЫ С ДИЗАЙНОМ ---

    @app.get("/", response_class=HTMLResponse)
    async def index(linkcode: str | None = Query(default=None)):
        code_display = linkcode if linkcode else "Код не указан"
        # Если кода нет, кнопка неактивна или ведет на главную
        auth_url = f"/auth/osu/login?linkcode={linkcode}" if linkcode else "#"
        
        return f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>Подтверждение - osu! Verifier</title>
            <style>
                body {{
                    background-color: #111;
                    color: white;
                    font-family: 'Segoe UI', Roboto, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                }}
                .card {{
                    background: #1e1e1e;
                    padding: 40px;
                    border-radius: 12px;
                    box-shadow: 0 8px 24px rgba(0,0,0,0.5);
                    text-align: center;
                    width: 100%;
                    max-width: 400px;
                }}
                h1 {{ font-size: 24px; margin-bottom: 20px; color: #fff; }}
                .desc {{ color: #aaa; font-size: 14px; margin-bottom: 30px; line-height: 1.5; }}
                .code-box {{
                    background: #000;
                    padding: 15px;
                    border-radius: 8px;
                    font-family: monospace;
                    font-size: 22px;
                    letter-spacing: 2px;
                    margin-bottom: 30px;
                    border: 1px solid #333;
                }}
                .btn {{
                    background: #fff;
                    color: #000;
                    padding: 14px;
                    text-decoration: none;
                    border-radius: 8px;
                    font-weight: bold;
                    display: block;
                    transition: 0.2s;
                }}
                .btn:hover {{ background: #ccc; transform: translateY(-2px); }}
                .btn.disabled {{ background: #333; color: #666; cursor: not-allowed; pointer-events: none; }}
            </style>
        </head>
        <body>
            <div class="card">
                <h1>Подтверждение</h1>
                <p class="desc">Вы перешли по ссылке для привязки вашего аккаунта <b>osu!</b> к серверу Discord.</p>
                <div class="code-box">{"/linkcode " if linkcode else ""}{code_display}</div>
                <p class="desc" style="font-size: 12px;">Если ваш код совпадает, нажмите кнопку ниже для безопасной авторизации.</p>
                <a href="{auth_url}" class="btn {'disabled' if not linkcode else ''}">Авторизоваться</a>
            </div>
        </body>
        </html>
        """

    @app.get("/auth/osu/login")
    async def osu_oauth_login(linkcode: str = Query(...)):
        state = secrets.token_urlsafe(32)
        conn = await get_db_conn()
        try:
            # Сохраняем linkcode в state, чтобы не потерять его после возврата от osu!
            await conn.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES ($1, $2, $3, $4)",
                state, 0, int(time.time()), int(time.time()) + 600,
            )
            # Временно сохраним связь state -> linkcode (можно через доп. колонку или кэш)
            # Для простоты добавим в URL редиректа state, который osu вернет нам
        finally:
            await conn.close()
        
        url = build_authorize_url(
            client_id=str(settings.osu_client_id),
            redirect_uri=settings.osu_redirect_uri,
            state=f"{state}:{linkcode}", # Передаем linkcode через state
            scope="public",
        )
        return RedirectResponse(url=url)

    @app.get("/auth/osu/callback")
    async def osu_oauth_callback(code: str, state: str) -> HTMLResponse:
        # Разделяем наш кастомный state
        raw_state, linkcode = state.split(":")
        
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow("SELECT * FROM oauth_osu_states WHERE state=$1", raw_state)
            if not row: return HTMLResponse("Ошибка сессии", status_code=400)
            
            await conn.execute("DELETE FROM oauth_osu_states WHERE state=$1", raw_state)
            
            tokens = await exchange_authorization_code(
                str(settings.osu_client_id), str(settings.osu_client_secret), code, settings.osu_redirect_uri
            )
            me = await fetch_me(tokens["access_token"])
            
            # Создаем челлендж
            challenge_id = await conn.fetchval(
                """INSERT INTO verification_challenges 
                (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                VALUES (0, $1, $2, $3, 'oauth', 'pending', $4, $5, 'oauth', $6)
                RETURNING id""",
                me["id"], me["username"], me.get("playmode", "osu"), 
                int(time.time()), int(time.time()) + 300, linkcode
            )

            # Страница успешной авторизации (Шаг 2)
            return HTMLResponse(f"""
                <body style="background:#111; color:white; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh;">
                    <div style="background:#1e1e1e; padding:40px; border-radius:12px; text-align:center;">
                        <h2 style="color:#ff66aa;">Почти готово, {me['username']}!</h2>
                        <p>Нажмите кнопку ниже, чтобы завершить привязку к Discord.</p>
                        <form action="/verify/form/finalize" method="post">
                            <input type="hidden" name="challenge_id" value="{challenge_id}">
                            <button type="submit" style="background:#ff66aa; border:none; color:white; padding:15px 30px; border-radius:8px; font-weight:bold; cursor:pointer;">ЗАВЕРШИТЬ</button>
                        </form>
                    </div>
                </body>
            """)
        finally:
            await conn.close()

    @app.post("/verify/form/finalize")
    async def finalize_from_form(challenge_id: int = Form(...)):
        try:
            await _finalize_challenge(challenge_id)
            return HTMLResponse("<h2>Успешно!</h2><p>Можете закрыть это окно и вернуться в Discord.</p>")
        except Exception as e:
            return HTMLResponse(f"<h2>Ошибка</h2><p>{str(e)}</p>")

    return app