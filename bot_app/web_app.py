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
            
            if not discord_id:
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

        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                """INSERT INTO pending_role_assignments 
                (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (discord_id, osu_id, osu_username, mode, digit, role_id, now),
            )
            await db.execute(
                "INSERT INTO osu_claims (osu_id, discord_id, claimed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(osu_id) DO UPDATE SET discord_id=excluded.discord_id, claimed_at=excluded.claimed_at",
                (osu_id, discord_id, now),
            )
            await db.execute("UPDATE verification_challenges SET status='completed' WHERE id=?", (challenge_id,))
            await db.commit()

        return {"ok": True, "role_id": role_id, "digit": digit, "mode": mode}

    # --- РОУТЫ ---

    @app.get("/", response_class=HTMLResponse)
    async def index(discord_id: int | None = Query(default=None)):
        pref = "" if discord_id is None else str(discord_id)
        return f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="https://cdn.tailwindcss.com"></script>
            <title>osu! Verification</title>
        </head>
        <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6 font-sans antialiased">
            <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2.5rem] p-10 space-y-8 shadow-2xl text-center">
                <div class="space-y-2">
                    <div class="inline-flex items-center justify-center px-4 py-2 rounded-2xl bg-gradient-to-br from-pink-500/20 to-violet-500/20 border border-white/10 mb-2">
                        <span class="text-sm font-black uppercase tracking-tighter italic bg-gradient-to-r from-pink-400 to-violet-400 bg-clip-text text-transparent">osu!</span>
                    </div>
                    <h1 class="text-4xl font-black uppercase tracking-tighter italic">Верификация osu!</h1>
                    <p class="text-gray-500 text-sm font-medium uppercase tracking-widest">Вход через osu! и роль по digit</p>
                </div>

                <ol class="text-left text-xs text-gray-500 space-y-2 border border-white/5 rounded-2xl p-4 bg-white/[0.02]">
                    <li class="flex gap-2"><span class="text-pink-400 font-bold shrink-0">1.</span> Укажите ваш числовой Discord ID (режим разработчика → ПКМ по профилю → «Скопировать ID»).</li>
                    <li class="flex gap-2"><span class="text-pink-400 font-bold shrink-0">2.</span> Нажмите кнопку ниже — откроется вход osu! (OAuth 2.0).</li>
                    <li class="flex gap-2"><span class="text-pink-400 font-bold shrink-0">3.</span> В Discord выполните <code class="text-violet-300">/linkcode</code> с кодом со страницы.</li>
                    <li class="flex gap-2"><span class="text-pink-400 font-bold shrink-0">4.</span> Нажмите «Завершить верификацию» — роль выдаётся по настроенному digit.</li>
                </ol>

                <form method="get" action="/auth/osu/login" class="space-y-4 text-left">
                    <div>
                        <label for="did" class="block text-[10px] uppercase font-bold tracking-[0.2em] text-gray-500 mb-2">Discord ID</label>
                        <input id="did" name="discord_id" type="number" min="1" step="1" required
                            placeholder="Например: 123456789012345678"
                            value="{pref}"
                            class="w-full h-14 px-5 rounded-2xl bg-black/40 border border-white/10 text-white placeholder:text-gray-600 focus:outline-none focus:ring-2 focus:ring-pink-500/50 focus:border-pink-500/40 font-mono text-sm" />
                    </div>
                    <button type="submit"
                        class="w-full bg-gradient-to-r from-pink-500 to-violet-600 text-white h-16 rounded-2xl font-black uppercase tracking-widest text-sm shadow-lg shadow-pink-500/20 hover:brightness-110 hover:scale-[1.02] transition-all active:scale-95">
                        Верифицировать через osu!
                    </button>
                </form>

                <div class="bg-gradient-to-br from-pink-500/10 to-violet-500/10 p-6 rounded-3xl border border-white/5">
                    <p class="text-gray-400 text-xs leading-relaxed text-center">
                        После входа osu! сервер считает <span class="text-white font-semibold">digit</span> по вашему рангу/ID (режим в <code class="text-pink-400/90">VERIFICATION_MODE</code>) и ставит в очередь соответствующую роль на Discord.
                    </p>
                </div>

                <div class="pt-2 border-t border-white/5">
                    <p class="text-[10px] text-gray-700 uppercase font-bold tracking-[0.2em]">Powered by osu! OAuth 2.0</p>
                </div>
            </div>
        </body>
        </html>
        """

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
        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute("SELECT discord_id FROM oauth_osu_states WHERE state=?", (state,)) as cursor:
                row = await cursor.fetchone()
            if not row:
                return HTMLResponse("Invalid state", status_code=400)
            await db.execute("DELETE FROM oauth_osu_states WHERE state=?", (state,))
            await db.commit()

        tokens = await exchange_authorization_code(str(settings.osu_client_id), str(settings.osu_client_secret), code, settings.osu_redirect_uri)
        me = await fetch_me(tokens["access_token"])
        osu_id, username, mode = int(me["id"]), me["username"], me.get("playmode", "osu")
        
        discord_link_code = secrets.token_hex(3).upper() 
        now = int(time.time())
        
        async with aiosqlite.connect(settings.database_path) as db:
            cursor = await db.execute(
                """INSERT INTO verification_challenges 
                (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                VALUES (0, ?, ?, ?, ?, 'pending', ?, ?, 'oauth', ?)""",
                (
                    osu_id,
                    username,
                    mode,
                    OAUTH_PROFILE_PLACEHOLDER,
                    now,
                    now + settings.verification_token_ttl_seconds,
                    discord_link_code,
                ),
            )
            challenge_id = cursor.lastrowid
            await db.commit()

        return HTMLResponse(content=f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1">
            <script src="https://cdn.tailwindcss.com"></script>
        </head>
        <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6 font-sans antialiased">
            <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2.5rem] p-8 space-y-6 shadow-2xl text-center">
                <div class="inline-flex px-4 py-2 rounded-2xl bg-gradient-to-br from-pink-500/20 to-violet-500/20 border border-white/10">
                    <span class="text-sm font-black uppercase tracking-tighter italic bg-gradient-to-r from-pink-400 to-violet-400 bg-clip-text text-transparent">osu!</span>
                </div>
                <h2 class="text-2xl font-bold uppercase tracking-tighter">Шаг 2: Discord</h2>
                <p class="text-sm text-gray-400">Аккаунт <b>{username}</b> подтвержден. Теперь привяжите Discord:</p>
                <div class="bg-white/5 p-6 rounded-2xl text-left border border-white/5">
                    <p class="text-[10px] uppercase text-gray-500 font-bold mb-2 tracking-widest">Команда для Discord</p>
                    <code class="text-2xl text-blue-400 font-mono font-bold">/linkcode {discord_link_code}</code>
                </div>
                <form method="post" action="/verify/form/finalize">
                    <input type="hidden" name="challenge_id" value="{challenge_id}" />
                    <button type="submit" class="w-full bg-white text-black h-16 rounded-2xl font-black uppercase tracking-widest hover:scale-[1.02] transition-all active:scale-95">
                        Завершить верификацию
                    </button>
                </form>
                <p class="text-[10px] text-gray-700 font-mono uppercase">SESSION ID: {challenge_id}</p>
            </div>
        </body>
        </html>
        """)

    @app.post("/verify/form/finalize", response_class=HTMLResponse)
    async def finalize_from_form(challenge_id: int = Form(...)):
        try:
            result = await _finalize_challenge(challenge_id)
            return HTMLResponse(content=f"""
            <!DOCTYPE html>
            <html lang="ru">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1">
                <script src="https://cdn.tailwindcss.com"></script>
            </head>
            <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6 font-sans antialiased">
                <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2.5rem] p-10 text-center space-y-4 shadow-2xl">
                    <div class="text-emerald-400 text-5xl mb-2">✓</div>
                    <h2 class="text-2xl font-black uppercase tracking-tighter italic">Успешно!</h2>
                    <p class="text-gray-400">Ваш <span class="text-pink-400 font-semibold">digit</span>: <span class="text-white font-bold text-2xl font-mono">{result['digit']}</span></p>
                    <p class="text-xs text-gray-500 uppercase tracking-widest">Режим: {result['mode']}</p>
                    <p class="text-sm text-gray-500 italic">Роль поставлена в очередь — выдача на сервере в течение минуты.</p>
                </div>
            </body>
            </html>
            """)
        except HTTPException as exc:
            return HTMLResponse(content=f"<body style='background:#09090b;color:red;padding:20px;font-family:sans-serif;'><h2>Ошибка</h2><p>{exc.detail}</p></body>", status_code=exc.status_code)

    return app
