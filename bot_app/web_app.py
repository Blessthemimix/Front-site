"""FastAPI app providing osu profile verification flow."""

from __future__ import annotations
import logging
import secrets
import time
from typing import Any

import aiosqlite
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

from .config import Settings
from .osu_client import OsuClient
from .rate_limiter import RateLimiter
from .verification import VerificationInput, compute_digit_value, extract_osu_identifier

logger = logging.getLogger(__name__)


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
    limiter = RateLimiter(max_per_minute=settings.rate_limit_per_minute)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: Exception):
        """Красивая страница ошибки вместо JSON логов."""
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
        <div class="p-10 md:p-12 bg-white/[0.01]">
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
                    Сначала пропишите <b class="text-blue-300">/linkcode</b> в Discord и вставьте код в описание профиля osu!.
                </div>
                <button type="submit" class="w-full bg-white text-black h-16 rounded-2xl font-black uppercase tracking-widest hover:scale-[1.02] active:scale-[0.98] transition-all cursor-pointer shadow-xl">
                    Верифицировать
                </button>
            </form>
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
<head>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>body {{ background-color: #09090b; color: white; font-family: sans-serif; }}</style>
</head>
<body class="min-h-screen flex items-center justify-center p-6">
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
        <p class="text-xs text-gray-400 leading-relaxed">
            Разместите токен выше в <b>описании (About Me)</b> вашего профиля osu!, затем завершите верификацию.
        </p>
        <p class="text-[10px] text-gray-600 font-mono uppercase tracking-widest">ID Сессии: {payload['challenge_id']}</p>
    </div>
</body>
</html>
"""

    @app.post("/verify/start")
    async def start_verification(
        request: Request, body: StartVerificationRequest
    ) -> dict[str, Any]:
        ip = request.client.host if request.client else "unknown"
        if not await limiter.allow(f"ip:{ip}") or not await limiter.allow(
            f"osu:{body.osu_identifier.lower()}"
        ):
            raise HTTPException(status_code=429, detail="Too many verification attempts")
        
        identifier = extract_osu_identifier(body.osu_identifier)
        osu_user = await osu_client.request(f"users/{identifier}")
        if not osu_user:
            raise HTTPException(status_code=404, detail="osu user not found")
        
        osu_id = int(osu_user["id"])
        username = str(osu_user["username"])
        mode = str(osu_user.get("playmode", "osu"))
        now = int(time.time())
        profile_token = secrets.token_urlsafe(6)
        discord_link_code = secrets.token_hex(3).upper()
        expires_at = now + settings.verification_token_ttl_seconds

        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute(
                "SELECT discord_id FROM osu_claims WHERE osu_id=?", (osu_id,)
            ) as cursor:
                claim = await cursor.fetchone()
            if claim and int(claim[0]) != body.discord_id:
                raise HTTPException(status_code=409, detail="This osu account is already linked")
            
            await db.execute(
                """
                INSERT INTO discord_link_codes (discord_id, code, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET code=excluded.code, expires_at=excluded.expires_at
                """,
                (body.discord_id, discord_link_code, now + settings.link_code_ttl_seconds),
            )
            cursor = await db.execute(
                """
                INSERT INTO verification_challenges (
                    discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (body.discord_id, osu_id, username, mode, profile_token, now, expires_at),
            )
            await db.commit()
            challenge_id = int(cursor.lastrowid)

        logger.info(
            "verification_started discord_id=%s osu_id=%s username=%s mode=%s",
            body.discord_id, osu_id, username, mode,
        )
        return {
            "challenge_id": challenge_id,
            "discord_link_code": discord_link_code,
            "profile_token": profile_token,
            "expires_at": expires_at,
        }

    @app.post("/verify/finalize")
    async def finalize_verification(body: FinalizeVerificationRequest) -> dict[str, Any]:
        now = int(time.time())
        async with aiosqlite.connect(settings.database_path) as db:
            async with db.execute(
                "SELECT id, discord_id, osu_id, osu_username, mode, profile_token, status, expires_at FROM verification_challenges WHERE id=?",
                (body.challenge_id,),
            ) as cursor:
                challenge = await cursor.fetchone()
            
            if not challenge:
                raise HTTPException(status_code=404, detail="Challenge not found")
            
            (challenge_id, discord_id, osu_id, osu_username, mode, profile_token, status, expires_at) = challenge
            
            if status != "pending":
                raise HTTPException(status_code=400, detail="Challenge already completed")
            if now > int(expires_at):
                raise HTTPException(status_code=400, detail="Challenge expired")
            
            async with db.execute(
                "SELECT verified_at FROM verified_discord_links WHERE discord_id=?", (discord_id,),
            ) as cursor:
                link_row = await cursor.fetchone()
            if not link_row:
                raise HTTPException(status_code=400, detail="Discord account not linked. Run /linkcode first.")

        osu_user = await osu_client.request(f"users/{osu_id}")
        if not osu_user:
            raise HTTPException(status_code=404, detail="osu user unavailable")
        
        page_raw = str((osu_user.get("page") or {}).get("raw") or "")
        if profile_token not in page_raw:
            raise HTTPException(status_code=400, detail="Profile token not found in osu profile bio")
        
        global_rank = (osu_user.get("statistics") or {}).get("global_rank")
        digit = compute_digit_value(
            VerificationInput(osu_id=int(osu_id), username=str(osu_username), global_rank=global_rank),
            settings.verification_mode,
            digit_modulus=settings.digit_modulus,
        )
        
        mode_map = role_mapping.get(str(mode), {})
        role_id = mode_map.get(digit)
        if not role_id:
            raise HTTPException(status_code=400, detail=f"No role mapping for mode={mode} digit={digit}")

        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO osu_claims (osu_id, discord_id, claimed_at) VALUES (?, ?, ?) ON CONFLICT(osu_id) DO UPDATE SET discord_id=excluded.discord_id, claimed_at=excluded.claimed_at",
                (osu_id, discord_id, now),
            )
            await db.execute(
                "INSERT INTO users (discord_id, osu_username, osu_id) VALUES (?, ?, ?) ON CONFLICT(discord_id) DO UPDATE SET osu_username=excluded.osu_username, osu_id=excluded.osu_id",
                (discord_id, osu_username, osu_id),
            )
            await db.execute(
                "INSERT INTO pending_role_assignments (discord_id, osu_id, osu_username, mode, digit_value, role_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (discord_id, osu_id, osu_username, mode, digit, role_id, now),
            )
            await db.execute("UPDATE verification_challenges SET status='completed' WHERE id=?", (challenge_id,))
            await db.commit()

        return {"ok": True, "digit": digit, "role_id": role_id}

    @app.post("/internal/role-assignment")
    async def internal_role_assignment(request: Request, body: InternalAssignRequest) -> dict[str, Any]:
        header_secret = request.headers.get("X-Webhook-Secret", "")
        if not secrets.compare_digest(header_secret, settings.webhook_secret):
            raise HTTPException(status_code=401, detail="Unauthorized")
        
        now = int(time.time())
        async with aiosqlite.connect(settings.database_path) as db:
            await db.execute(
                "INSERT INTO pending_role_assignments (discord_id, osu_id, osu_username, mode, digit_value, role_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body.discord_id, body.osu_id, body.osu_username, body.mode, body.digit, body.role_id, now),
            )
            await db.commit()
        return {"ok": True}

    return app