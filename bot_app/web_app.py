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

class InternalAssignRequest(BaseModel):
    discord_id: int = Field(gt=0)
    osu_id: int = Field(gt=0)
    osu_username: str
    mode: str
    digit: int = Field(ge=0)
    role_id: int = Field(gt=0)

def create_web_app(
    *,
    settings: Settings,
    osu_client: OsuClient,
    role_mapping: dict[str, dict[int, int]],
) -> FastAPI:
    """Build FastAPI app with verification endpoints."""
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

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: Exception):
        if "/form/" in request.url.path:
            return HTMLResponse(
                content=f"""
                <!DOCTYPE html>
                <html lang="ru">
                <head><script src="https://cdn.tailwindcss.com"></script></head>
                <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6 font-sans">
                    <div class="max-w-md w-full bg-[#0c0c0e] border border-red-500/20 rounded-[2rem] p-10 text-center shadow-2xl">
                        <div class="w-16 h-16 bg-red-500/10 rounded-full flex items-center justify-center mx-auto mb-6">
                            <span class="text-red-500 text-3xl">!</span>
                        </div>
                        <h2 class="text-2xl font-black uppercase tracking-tighter mb-4">Ошибка ввода</h2>
                        <p class="text-gray-400 mb-8 leading-relaxed">Discord ID должен состоять только из цифр. Пожалуйста, проверьте данные.</p>
                        <a href="/" class="inline-block bg-white text-black px-8 py-4 rounded-xl font-bold uppercase text-xs tracking-widest hover:scale-105 transition-all">
                            Попробовать снова
                        </a>
                    </div>
                </body>
                </html>
                """,
                status_code=422
            )
        return await request.app.default_exception_handler(request, exc)

    async def _finalize_challenge(challenge_id: int) -> dict[str, Any]:
        now = int(time.time())
        if not settings.osu_client_id or not settings.osu_client_secret:
            raise HTTPException(
                status_code=503,
                detail="OSU API credentials are not configured. Contact the administrator.",
            )
        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute(
                """
                SELECT discord_id, osu_id, osu_username, mode, profile_token, expires_at,
                       IFNULL(verification_source, 'bio')
                FROM verification_challenges WHERE id=?
                """,
                (challenge_id,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="challenge not found")
            (
                discord_id,
                osu_id,
                osu_username,
                mode,
                profile_token,
                expires_at,
                verification_source,
            ) = row
            if now > expires_at:
                raise HTTPException(status_code=410, detail="challenge expired")
            async with db.execute(
                "SELECT 1 FROM verified_discord_links WHERE discord_id=?",
                (discord_id,),
            ) as cur:
                if not await cur.fetchone():
                    raise HTTPException(
                        status_code=403,
                        detail="Discord not verified: run /linkcode in Discord first",
                    )

        full_user = await osu_client.request(f"users/{osu_id}")
        if not full_user:
            raise HTTPException(status_code=502, detail="osu! API error")
        if verification_source != "oauth":
            raw = (full_user.get("page") or {}).get("raw") or ""
            if profile_token not in raw:
                raise HTTPException(
                    status_code=400,
                    detail="profile token not found in osu! bio",
                )
        stats = full_user.get("statistics") or {}
        global_rank = stats.get("global_rank")
        vinput = VerificationInput(
            osu_id=int(osu_id),
            username=str(osu_username),
            global_rank=global_rank,
        )
        try:
            digit = compute_digit_value(
                vinput,
                settings.verification_mode,
                digit_modulus=settings.digit_modulus,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        role_id = role_mapping.get(mode, {}).get(digit)
        if not role_id:
            raise HTTPException(
                status_code=400,
                detail=f"No role configured for mode={mode!r} digit={digit}",
            )
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                """
                INSERT INTO pending_role_assignments
                (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (discord_id, osu_id, osu_username, mode, digit, role_id, now),
            )
            await db.execute(
                """
                INSERT INTO osu_claims (osu_id, discord_id, claimed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(osu_id) DO UPDATE SET
                    discord_id=excluded.discord_id,
                    claimed_at=excluded.claimed_at
                """,
                (osu_id, discord_id, now),
            )
            await db.execute(
                "UPDATE verification_challenges SET status=? WHERE id=?",
                ("completed", challenge_id),
            )
            await db.commit()
        return {
            "ok": True,
            "role_id": role_id,
            "digit": digit,
            "mode": mode,
        }

    @app.post("/verify/form/finalize")
async def finalize_verification(data: dict):
    session_id = data.get("session_id") # ID сессии: 1 со скриншота
    
    # 1. Достаем сессию из БД
    challenge = await db.get_challenge_by_id(session_id)
    
    # 2. Проверяем, привязан ли уже Discord (после команды /linkcode)
    if not challenge or not challenge.get("discord_id"):
        return {"status": "error", "message": "Сначала введите команду /linkcode в Discord!"}

    # 3. Если всё ок, рассчитываем DIGIT и выдаем роль
    # (Здесь твоя существующая логика из compute_digit_value)
    digit = compute_digit_value(challenge['osu_id'])
    
    # Добавляем задачу на выдачу роли в очередь
    await db.add_to_pending_roles(challenge['discord_id'], digit)
    
    return {"status": "success", "message": "Верификация завершена! Роль скоро будет выдана."}

    @app.get("/auth/osu/login")
    async def osu_oauth_login(
        request: Request,
        discord_id: int = Query(..., gt=0),
    ) -> RedirectResponse:
        if not settings.osu_client_id or not settings.osu_client_secret:
            raise HTTPException(
                status_code=503,
                detail="OSU API credentials are not configured",
            )
        ip = request.client.host if request.client else "unknown"
        if not await limiter.allow(f"oauth_ip:{ip}"):
            raise HTTPException(status_code=429, detail="Too many attempts")
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        expires = now + settings.verification_token_ttl_seconds
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (state, discord_id, now, expires),
            )
            await db.commit()
        url = build_authorize_url(
            client_id=str(settings.osu_client_id),
            redirect_uri=settings.osu_redirect_uri,
            state=state,
            scope="public",
        )
        return RedirectResponse(url=url, status_code=302)

    @app.get("/auth/osu/callback")
    async def osu_oauth_callback(
        request: Request,
        code: str | None = None,
        state: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        if error:
            return HTMLResponse(
                content=f"<html><body><p>osu! OAuth error: {error}</p><a href='/'>Home</a></body></html>",
                status_code=400,
            )
        if not code or not state:
            return HTMLResponse(
                content="<html><body><p>Missing code or state</p><a href='/'>Home</a></body></html>",
                status_code=400,
            )
        if not settings.osu_client_id or not settings.osu_client_secret:
            raise HTTPException(status_code=503, detail="OSU API not configured")
        now = int(time.time())
        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute(
                "SELECT discord_id, expires_at FROM oauth_osu_states WHERE state=?",
                (state,),
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return HTMLResponse(
                    content="<html><body><p>Invalid or expired state</p><a href='/'>Home</a></body></html>",
                    status_code=400,
                )
            discord_id, exp = row
            if now > exp:
                await db.execute("DELETE FROM oauth_osu_states WHERE state=?", (state,))
                await db.commit()
                return HTMLResponse(
                    content="<html><body><p>OAuth session expired</p><a href='/'>Home</a></body></html>",
                    status_code=400,
                )
            await db.execute("DELETE FROM oauth_osu_states WHERE state=?", (state,))
            await db.commit()

        try:
            tokens = await exchange_authorization_code(
                client_id=str(settings.osu_client_id),
                client_secret=str(settings.osu_client_secret),
                code=code,
                redirect_uri=settings.osu_redirect_uri,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("osu oauth token exchange failed")
            return HTMLResponse(
                content=f"<html><body><p>Token exchange failed: {exc}</p><a href='/'>Home</a></body></html>",
                status_code=502,
            )
        access = tokens.get("access_token")
        if not access:
            return HTMLResponse(
                content="<html><body><p>No access_token in response</p><a href='/'>Home</a></body></html>",
                status_code=502,
            )
        me = await fetch_me(access)
        if not me:
            return HTMLResponse(
                content="<html><body><p>Could not load osu! profile (/me)</p><a href='/'>Home</a></body></html>",
                status_code=502,
            )
        osu_id = int(me["id"])
        username = str(me["username"])
        mode = str(me.get("playmode", "osu"))
        discord_link_code = secrets.token_hex(3).upper()
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO discord_link_codes (discord_id, code, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at",
                (discord_id, discord_link_code, now + settings.link_code_ttl_seconds),
            )
            cursor = await db.execute(
                """
                INSERT INTO verification_challenges
                (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, 'oauth')
                """,
                (
                    discord_id,
                    osu_id,
                    username,
                    mode,
                    OAUTH_PROFILE_PLACEHOLDER,
                    now,
                    now + settings.verification_token_ttl_seconds,
                ),
            )
            await db.commit()
            challenge_id = int(cursor.lastrowid)

        return HTMLResponse(
            content=f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head><script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6">
            <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2rem] p-8 space-y-6 shadow-2xl text-center">
                <h2 class="text-2xl font-bold uppercase tracking-tighter">Шаг 2: Discord</h2>
                <p class="text-sm text-gray-400 text-left">osu! аккаунт подтверждён через OAuth. Подтвердите Discord:</p>
                <div class="space-y-4 text-left">
                    <div class="bg-white/5 p-4 rounded-xl">
                        <p class="text-[10px] uppercase text-gray-500 font-bold mb-1">Код для Discord</p>
                        <code class="text-xl text-blue-400 font-mono">/linkcode {discord_link_code}</code>
                    </div>
                </div>
                <p class="text-xs text-gray-400 leading-relaxed text-left">
                    Выполните команду в Discord, затем нажмите кнопку ниже — роль будет поставлена в очередь по вашему DIGIT.
                </p>
                <form method="post" action="/verify/form/finalize">
                    <input type="hidden" name="challenge_id" value="{challenge_id}" />
                    <button type="submit" class="w-full bg-white text-black h-14 rounded-2xl font-black uppercase tracking-widest">
                        Завершить верификацию
                    </button>
                </form>
                <p class="text-[10px] text-gray-600 font-mono uppercase">ID сессии: {challenge_id}</p>
            </div>
        </body>
        </html>
        """
        )

    @app.get("/api/auth/osu/login-url")
    async def api_osu_login_url(
        request: Request,
        discord_id: int = Query(..., gt=0),
    ) -> dict[str, str]:
        """JSON helper for SPA: returns absolute authorize URL (same as /auth/osu/login)."""
        if not settings.osu_client_id or not settings.osu_client_secret:
            raise HTTPException(status_code=503, detail="OSU API not configured")
        ip = request.client.host if request.client else "unknown"
        if not await limiter.allow(f"oauth_ip:{ip}"):
            raise HTTPException(status_code=429, detail="Too many attempts")
        state = secrets.token_urlsafe(32)
        now = int(time.time())
        expires = now + settings.verification_token_ttl_seconds
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (state, discord_id, now, expires),
            )
            await db.commit()
        url = build_authorize_url(
            client_id=str(settings.osu_client_id),
            redirect_uri=settings.osu_redirect_uri,
            state=state,
            scope="public",
        )
        return {"authorize_url": url}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return """
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>osu! Verification</title>
            <script src="https://cdn.tailwindcss.com"></script>
            <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;900&display=swap" rel="stylesheet">
            <style>
                body { font-family: 'Inter', sans-serif; background-color: #09090b; color: white; margin: 0; }
                .grid-bg {
                    background-image: linear-gradient(to right, rgba(255,255,255,0.05) 1px, transparent 1px),
                                      linear-gradient(to bottom, rgba(255,255,255,0.05) 1px, transparent 1px);
                    background-size: 40px 40px;
                }
            </style>
        </head>
        <body class="min-h-screen flex items-center justify-center p-4 md:p-10 relative overflow-hidden">
            <div class="absolute inset-0 grid-bg z-0 [mask-image:radial-gradient(ellipse_at_center,black,transparent)]"></div>
            <div class="relative z-10 w-full max-w-[900px] grid grid-cols-1 md:grid-cols-2 bg-[#0c0c0e] border border-white/5 rounded-[2.5rem] overflow-hidden shadow-2xl">
                <div class="p-10 md:p-14 flex flex-col justify-center border-b md:border-b-0 md:border-r border-white/5">
                    <h1 class="text-6xl font-black italic tracking-tighter uppercase mb-4 text-white">Verify</h1>
                    <p class="text-gray-400 font-medium leading-relaxed">
                        Свяжите свои аккаунты Discord и <span class="text-white">osu!</span> для автоматического получения ролей.
                    </p>
                    <div class="mt-8 flex items-center gap-3 text-[10px] uppercase tracking-[0.3em] text-gray-500 font-bold">
                        <span class="w-2 h-2 bg-green-500 rounded-full shadow-[0_0_10px_rgba(34,197,94,0.5)]"></span>
                        System Online
                    </div>
                </div>
                <div class="p-10 md:p-12 bg-white/[0.01] space-y-10">
                    <div>
                        <p class="text-[10px] uppercase tracking-widest text-gray-500 font-bold mb-4">Способ 1 — osu! OAuth</p>
                        <form method="get" action="/auth/osu/login" class="space-y-6">
                            <div class="space-y-2">
                                <label class="text-[10px] uppercase tracking-[0.2em] font-bold text-gray-500 ml-1">Discord ID</label>
                                <input type="text" name="discord_id" required
                                    inputmode="numeric" pattern="[0-9]+"
                                    title="Введите только цифры (ваш Discord ID)"
                                    placeholder="Напр: 123456789..."
                                    class="w-full h-14 bg-white/[0.03] border border-white/10 rounded-2xl px-5 outline-none focus:border-white/30 focus:bg-white/[0.05] transition-all text-white placeholder:text-gray-700">
                            </div>
                            <p class="text-[11px] text-gray-500 leading-relaxed">
                                Откроется страница osu!, вы войдёте как в приложении. Токен в профиле не нужен.
                            </p>
                            <button type="submit" class="w-full bg-white text-black h-16 rounded-2xl font-black uppercase tracking-widest hover:scale-[1.02] active:scale-[0.98] transition-all cursor-pointer shadow-xl">
                                Войти через osu!
                            </button>
                        </form>
                    </div>
                    <div class="border-t border-white/5 pt-10">
                        <p class="text-[10px] uppercase tracking-widest text-gray-500 font-bold mb-4">Способ 2 — ник и токен в bio</p>
                        <form method="post" action="/verify/form/start" class="space-y-6">
                            <div class="space-y-2">
                                <label class="text-[10px] uppercase tracking-[0.2em] font-bold text-gray-500 ml-1">Discord ID</label>
                                <input type="text" name="discord_id" required
                                    inputmode="numeric" pattern="[0-9]+"
                                    title="Введите только цифры (ваш Discord ID)"
                                    placeholder="Напр: 123456789..."
                                    class="w-full h-14 bg-white/[0.03] border border-white/10 rounded-2xl px-5 outline-none focus:border-white/30 focus:bg-white/[0.05] transition-all text-white placeholder:text-gray-700">
                            </div>
                            <div class="space-y-2">
                                <label class="text-[10px] uppercase tracking-[0.2em] font-bold text-gray-500 ml-1">osu! Username / URL</label>
                                <input type="text" name="osu_identifier" required placeholder="Ник или ссылка"
                                    class="w-full h-14 bg-white/[0.03] border border-white/10 rounded-2xl px-5 outline-none focus:border-white/30 focus:bg-white/[0.05] transition-all text-white placeholder:text-gray-700">
                            </div>
                            <div class="bg-[#1e1b4b]/30 border border-blue-500/20 rounded-2xl p-4 text-[11px] text-blue-200/60 leading-relaxed italic">
                                После шага 2 вставьте токен в <b>About Me</b> на osu!, затем завершите верификацию.
                            </div>
                            <button type="submit" class="w-full bg-zinc-800 text-white h-14 rounded-2xl font-bold uppercase tracking-widest hover:bg-zinc-700 transition-all cursor-pointer">
                                Дальше (классика)
                            </button>
                        </form>
                    </div>
                </div>
            </div>
        </body>
        </html>
        """

    @app.post("/verify/form/start", response_class=HTMLResponse)
    async def start_from_form(
        request: Request, discord_id: int = Form(...), osu_identifier: str = Form(...)
    ) -> str:
        payload = await start_verification(
            request, StartVerificationRequest(discord_id=discord_id, osu_identifier=osu_identifier)
        )
        return f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head><script src="https://cdn.tailwindcss.com"></script></head>
        <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6">
            <div class="w-full max-w-md bg-[#0c0c0e] border border-white/5 rounded-[2rem] p-8 space-y-6 shadow-2xl text-center">
                <h2 class="text-2xl font-bold uppercase tracking-tighter">Шаг 2: Подтверждение</h2>
                <div class="space-y-4 text-left">
                    <div class="bg-white/5 p-4 rounded-xl">
                        <p class="text-[10px] uppercase text-gray-500 font-bold mb-1">Код для Discord</p>
                        <code class="text-xl text-blue-400 font-mono">/linkcode {payload['discord_link_code']}</code>
                    </div>
                    <div class="bg-white/5 p-4 rounded-xl">
                        <p class="text-[10px] uppercase text-gray-500 font-bold mb-1">Токен для профиля osu!</p>
                        <code class="text-sm text-green-400 break-all font-mono">{payload['profile_token']}</code>
                    </div>
                </div>
                <p class="text-xs text-gray-400 leading-relaxed">Разместите токен в <b>About Me</b> профиля osu!, выполните <b>/linkcode</b> в Discord, затем нажмите кнопку.</p>
                <form method="post" action="/verify/form/finalize">
                    <input type="hidden" name="challenge_id" value="{payload['challenge_id']}" />
                    <button type="submit" class="w-full bg-white text-black h-14 rounded-2xl font-black uppercase tracking-widest mt-4">
                        Завершить верификацию
                    </button>
                </form>
                <p class="text-[10px] text-gray-600 font-mono uppercase">ID Сессии: {payload['challenge_id']}</p>
            </div>
        </body>
        </html>
        """

    @app.post("/verify/start")
    async def start_verification(request: Request, body: StartVerificationRequest) -> dict[str, Any]:
        if not settings.osu_client_id or not settings.osu_client_secret:
            raise HTTPException(
                status_code=503,
                detail="OSU API credentials are not configured. Contact the administrator.",
            )
        ip = request.client.host if request.client else "unknown"
        if not await limiter.allow(f"ip:{ip}") or not await limiter.allow(f"osu:{body.osu_identifier.lower()}"):
            raise HTTPException(status_code=429, detail="Too many attempts")
        
        identifier = extract_osu_identifier(body.osu_identifier)
        osu_user = await osu_client.request(f"users/{identifier}")
        if not osu_user:
            raise HTTPException(status_code=404, detail="osu user not found")
        
        osu_id, username = int(osu_user["id"]), str(osu_user["username"])
        mode = str(osu_user.get("playmode", "osu"))
        now = int(time.time())
        profile_token, discord_link_code = secrets.token_urlsafe(6), secrets.token_hex(3).upper()

        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO discord_link_codes (discord_id, code, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(discord_id) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at",
                (body.discord_id, discord_link_code, now + settings.link_code_ttl_seconds),
            )
            cursor = await db.execute(
                "INSERT INTO verification_challenges (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
                (body.discord_id, osu_id, username, mode, profile_token, now, now + settings.verification_token_ttl_seconds),
            )
            await db.commit()
            challenge_id = int(cursor.lastrowid)

        return {"challenge_id": challenge_id, "discord_link_code": discord_link_code, "profile_token": profile_token}

    @app.post("/verify/finalize")
    async def finalize_verification(body: FinalizeVerificationRequest) -> dict[str, Any]:
        return await _finalize_challenge(body.challenge_id)

    @app.post("/verify/form/finalize", response_class=HTMLResponse)
    async def finalize_from_form(challenge_id: int = Form(...)) -> HTMLResponse:
        try:
            result = await _finalize_challenge(challenge_id)
        except HTTPException as exc:
            return HTMLResponse(
                content=f"""
                <!DOCTYPE html>
                <html lang="ru">
                <head><script src="https://cdn.tailwindcss.com"></script></head>
                <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6 font-sans">
                    <div class="max-w-md w-full bg-[#0c0c0e] border border-red-500/20 rounded-[2rem] p-10 text-center shadow-2xl">
                        <h2 class="text-xl font-bold mb-4">Ошибка</h2>
                        <p class="text-gray-400 mb-6">{exc.detail}</p>
                        <a href="/" class="inline-block bg-white text-black px-8 py-4 rounded-xl font-bold uppercase text-xs">На главную</a>
                    </div>
                </body>
                </html>
                """,
                status_code=exc.status_code,
            )
        rid = result.get("role_id")
        digit = result.get("digit")
        return HTMLResponse(
            content=f"""
            <!DOCTYPE html>
            <html lang="ru">
            <head><script src="https://cdn.tailwindcss.com"></script></head>
            <body class="bg-[#09090b] text-white min-h-screen flex items-center justify-center p-6">
                <div class="w-full max-w-md bg-[#0c0c0e] border border-green-500/20 rounded-[2rem] p-8 space-y-4 text-center shadow-2xl">
                    <h2 class="text-2xl font-bold uppercase tracking-tighter text-green-400">Готово</h2>
                    <p class="text-gray-400 text-sm">Роль поставлена в очередь (DIGIT <b>{digit}</b>). Бот обработает в течение нескольких секунд.</p>
                    <p class="text-[10px] text-gray-600 font-mono">role_id={rid}</p>
                    <a href="/" class="inline-block mt-4 text-blue-400 underline text-sm">На главную</a>
                </div>
            </body>
            </html>
            """
        )

    return app
