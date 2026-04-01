"""FastAPI app providing osu profile verification flow."""

from __future__ import annotations
import logging
import secrets
import time
from typing import Any

import aiosqlite
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
        async with aiosqlite.connect(settings.database_path) as db:
            # Ищем сессию и ПРОВЕРЯЕМ, привязан ли discord_id (через бота)
            async with db.execute(
                """
                SELECT discord_id, osu_id, osu_username, mode, profile_token, expires_at,
                       IFNULL(verification_source, 'bio'), status
                FROM verification_challenges WHERE id=?
                """,
                (challenge_id,),
            ) as cursor:
                row = await cursor.fetchone()
            
            if not row:
                raise HTTPException(status_code=404, detail="Сессия не найдена.")
            
            (discord_id, osu_id, osu_username, mode, profile_token, 
             expires_at, verification_source, status) = row

            if status == "completed":
                raise HTTPException(status_code=400, detail="Верификация уже завершена.")
            if now > expires_at:
                raise HTTPException(status_code=410, detail="Срок действия сессии истек.")
            
            # Проверка: выполнил ли пользователь /linkcode в Дискорде?
            # В обновленной логике бот записывает discord_id прямо в verification_challenges
            if not discord_id:
                raise HTTPException(
                    status_code=403, 
                    detail="Discord не привязан. Сначала введите команду /linkcode в Discord."
                )

        # Получаем актуальные данные игрока
        full_user = await osu_client.request(f"users/{osu_id}")
        if not full_user:
            raise HTTPException(status_code=502, detail="Ошибка API osu!")

        # Если это не OAuth, проверяем токен в био
        if verification_source != "oauth":
            raw_bio = (full_user.get("page") or {}).get("raw") or ""
            if profile_token not in raw_bio:
                raise HTTPException(status_code=400, detail="Токен не найден в About Me вашего профиля.")

        # Расчет DIGIT и роли
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

        async with aiosqlite.connect(settings.database_path) as db:
            # Ставим в очередь на выдачу роли
            await db.execute(
                """INSERT INTO pending_role_assignments 
                (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (discord_id, osu_id, osu_username, mode, digit, role_id, now),
            )
            # Записываем клейм
            await db.execute(
                "INSERT INTO osu_claims (osu_id, discord_id, claimed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(osu_id) DO UPDATE SET discord_id=excluded.discord_id, claimed_at=excluded.claimed_at",
                (osu_id, discord_id, now),
            )
            # Закрываем челендж
            await db.execute("UPDATE verification_challenges SET status='completed' WHERE id=?", (challenge_id,))
            await db.commit()

        return {"ok": True, "role_id": role_id, "digit": digit, "mode": mode}

    # --- РОУТЫ ---

    @app.get("/auth/osu/login")
    async def osu_oauth_login(request: Request, discord_id: int = Query(..., gt=0)):
        state = secrets.token_urlsafe(32)
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (state, discord_id, int(time.time()), int(time.time()) + 600),
            )
            await db.commit()
        
        url = build_authorize_url(
            client_id=str(settings.osu_client_id),
            redirect_uri=settings.osu_redirect_uri,
            state=state,
            scope="public",
        )
        return RedirectResponse(url=url)

    @app.get("/auth/osu/callback")
    async def osu_oauth_callback(request: Request, code: str | None = None, state: str | None = None) -> HTMLResponse:
        # 1. Проверка state
        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute("SELECT discord_id FROM oauth_osu_states WHERE state=?", (state,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return HTMLResponse("Invalid state", status_code=400)
            initial_discord_id = row[0]
            await db.execute("DELETE FROM oauth_osu_states WHERE state=?", (state,))
            await db.commit()

        # 2. Обмен токена и получение инфо об игроке
        tokens = await exchange_authorization_code(str(settings.osu_client_id), str(settings.osu_client_secret), code, settings.osu_redirect_uri)
        me = await fetch_me(tokens["access_token"])
        
        osu_id, username, mode = int(me["id"]), me["username"], me.get("playmode", "osu")
        
        # 3. Генерируем рандомный код для Discord бота (тот самый EF97A4)
        discord_link_code = secrets.token_hex(3).upper() 
        now = int(time.time())
        
        async with aiosqlite.connect(settings.database_path) as db:
            # Создаем челендж БЕЗ discord_id (его заполнит бот по коду)
            cursor = await db.execute(
                """INSERT INTO verification_challenges 
                (osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                VALUES (?, ?, ?, ?, 'pending', ?, ?, 'oauth', ?)""",
                (osu_id, username, mode, OAUTH_PROFILE_PLACEHOLDER, now, now + settings.verification_token_ttl_seconds, discord_link_code),
            )
            challenge_id = cursor.lastrowid
            await db.commit()

        # 4. Показываем Шаг 2 пользователю
        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head><script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6">
            <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2rem] p-8 space-y-6 shadow-2xl text-center">
                <h2 class="text-2xl font-bold uppercase tracking-tighter">Шаг 2: Discord</h2>
                <p class="text-sm text-gray-400">Аккаунт <b>{username}</b> подтвержден. Теперь привяжите Discord:</p>
                <div class="bg-white/5 p-4 rounded-xl text-left">
                    <p class="text-[10px] uppercase text-gray-500 font-bold mb-1">Команда для Discord</p>
                    <code class="text-xl text-blue-400 font-mono">/linkcode {discord_link_code}</code>
                </div>
                <form method="post" action="/verify/form/finalize">
                    <input type="hidden" name="challenge_id" value="{challenge_id}" />
                    <button type="submit" class="w-full bg-white text-black h-14 rounded-2xl font-black uppercase tracking-widest hover:scale-105 transition-all">
                        Завершить верификацию
                    </button>
                </form>
                <p class="text-[10px] text-gray-600 font-mono">ID СЕССИИ: {challenge_id}</p>
            </div>
        </body>
        </html>
        """)

    # Остальные роуты (index, finalize_from_form) оставляем как были, 
    # так как они используют общую функцию _finalize_challenge.
    
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return """
        <html>
            <body style="background: #09090b; color: white; display: flex; justify-center: center; align-items: center; height: 100vh; font-family: sans-serif;">
                <div style="text-align: center;">
                    <h1>Верификация osu!</h1>
                    <p>Для начала верификации используйте ссылку из Discord бота.</p>
                </div>
            </body>
        </html>
        """

    @app.post("/verify/form/finalize", response_class=HTMLResponse)
    async def finalize_from_form(challenge_id: int = Form(...)):
        try:
            result = await _finalize_challenge(challenge_id)
            return HTMLResponse(content=f"Успех! Ваш DIGIT: {result['digit']}") # Упростил для примера
        except HTTPException as exc:
            return HTMLResponse(content=f"Ошибка: {exc.detail}", status_code=exc.status_code)

    return app
