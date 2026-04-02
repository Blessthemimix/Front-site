from __future__ import annotations
import logging
import secrets
import time
from fastapi import FastAPI, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from .config import Settings
from .osu_client import OsuClient
from .osu_oauth import build_authorize_url, exchange_authorization_code, fetch_me
from .db import get_db_conn
from .verification import VerificationInput, compute_digit_value
import os
import typing

logger = logging.getLogger(__name__)

try:
    from supabase import create_client, Client  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    # In local dev/tests supabase might be absent. The web app must still import.
    create_client = None  # type: ignore[assignment]
    Client = typing.Any  # type: ignore[misc,assignment]

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# IMPORTANT:
# Supabase keys are optional during dev/preview. On Render a missing/invalid key
# must not crash the whole web service at import time.
supabase: Client | None = None


def get_supabase() -> Client | None:
    global supabase
    if supabase is not None:
        return supabase
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    if create_client is None:
        return None
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception:  # noqa: BLE001
        logger.warning("Supabase init failed (invalid keys or URL). Continuing without Supabase.")
        supabase = None
    return supabase

def create_web_app(*, settings: Settings, osu_client: OsuClient, role_mapping: dict[str, dict[int, int]]) -> FastAPI:
    app = FastAPI()

    # Шаблон главной страницы
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>osu! Verify</title>
        <style>
            body {
                margin: 0; padding: 0;
                display: flex; justify-content: center; align-items: center;
                min-height: 100vh;
                color: white; font-family: 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #000 0%, #151515 100%);
                position: relative;
            }
            body::before {
                content: "";
                position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                background-image: linear-gradient(0deg, transparent 24%, rgba(255, 255, 255, .02) 25%, rgba(255, 255, 255, .02) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .02) 75%, rgba(255, 255, 255, .02) 76%, transparent 77%, transparent), 
                                 linear-gradient(90deg, transparent 24%, rgba(255, 255, 255, .02) 25%, rgba(255, 255, 255, .02) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .02) 75%, rgba(255, 255, 255, .02) 76%, transparent 77%, transparent);
                background-size: 60px 60px;
                z-index: -1;
            }
            .container {
                display: flex;
                width: 980px; height: 640px;
                background: #111;
                border-radius: 24px;
                overflow: hidden;
                border: 1px solid #222;
                box-shadow: 0 30px 70px rgba(0,0,0,0.9);
            }
            .left-panel {
                flex: 1;
                padding: 60px;
                display: flex;
                flex-direction: column;
                justify-content: center;
                background: #0d0d0d;
                border-right: 1px solid #222;
            }
            .right-panel {
                flex: 1.3;
                padding: 45px 55px;
                overflow-y: auto;
                background: #111;
            }
            .logo { 
                font-size: 64px; 
                font-weight: 900; 
                font-style: italic; 
                margin-bottom: 25px; 
                letter-spacing: -1px; 
                color: #fff;
                line-height: 1;
            }
            .status { font-size: 13px; color: #4ade80; display: flex; align-items: center; gap: 10px; text-transform: uppercase; font-weight: 700; }
            .status::before { content: ''; width: 10px; height: 10px; background: #4ade80; border-radius: 50%; box-shadow: 0 0 10px #4ade80; }
            
            h3 { font-size: 11px; color: #666; text-transform: uppercase; margin: 35px 0 20px 0; letter-spacing: 1.5px; font-weight: 700; }
            h3:first-child { margin-top: 0; }
            
            .input-group { margin-bottom: 25px; }
            label { display: block; font-size: 12px; color: #888; margin-bottom: 10px; text-transform: uppercase; font-weight: 700; }
            .label-help { font-size: 10px; color: #555; text-transform: none; margin-left: 5px; font-weight: 400; display: block; margin-top: 4px; }
            
            input {
                width: 100%;
                background: #181818;
                border: 1px solid #333;
                padding: 16px;
                border-radius: 10px;
                color: white;
                box-sizing: border-box;
                outline: none;
                transition: all 0.3s ease;
                font-size: 15px;
            }
            input:focus { border-color: #555; background: #1c1c1c; box-shadow: 0 0 15px rgba(255,255,255,0.02); }
            
            .btn {
                width: 100%;
                padding: 18px;
                border-radius: 10px;
                font-weight: 800;
                cursor: pointer;
                border: none;
                text-transform: uppercase;
                transition: 0.3s cubic-bezier(0.4, 0, 0.2, 1);
                text-decoration: none;
                display: inline-block;
                text-align: center;
                font-size: 14px;
            }
            .btn-primary { background: #fff; color: #000; }
            .btn-secondary { background: #1e1e1e; color: #999; border: 1px solid #333; }
            .btn:hover { transform: translateY(-3px); filter: brightness(1.1); box-shadow: 0 10px 20px rgba(0,0,0,0.4); }
            
            .divider { height: 1px; background: #222; margin: 35px 0; position: relative; }
            .info-box { background: #141420; padding: 18px; border-radius: 10px; font-size: 12px; color: #7a7a9e; border: 1px solid #1e1e35; line-height: 1.6; }
            b { color: #eee; font-weight: 700; }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="left-panel">
                <div class="logo">VERIFY</div>
                <p style="color: #888; line-height: 1.7; font-size: 16px; margin-bottom: 35px;">
                    Используйте этот сервис для <b>привязки</b> вашего аккаунта <b>osu!</b> к Discord для <b>автоматического</b> получения игровых ролей.
                </p>
                <div class="status">System Online</div>
            </div>
            <div class="right-panel">
                <h3>Способ 1 — osu! OAuth (Рекомендуется)</h3>
                <form action="/auth/osu/login" method="get">
                    <div class="input-group">
                        <label>Discord ID <span class="label-help">Настройки → Расширенные → Режим разработчика (ВКЛ) → ПКМ по себе → Скопировать ID</span></label>
                        <input type="text" name="discord_id" value="{{DISCORD_ID}}" placeholder="Напр: 1160688626934497481" required>
                    </div>
                    <button type="submit" class="btn btn-primary">Войти через osu!</button>
                </form>

                <div class="divider"></div>

                <h3>Способ 2 — Ручная проверка</h3>
                <form action="/verify/classic/start" method="post">
                    <div class="input-group">
                        <label>Discord ID</label>
                        <input type="text" name="discord_id" value="{{DISCORD_ID}}" placeholder="Ваш 18-значный ID" required>
                    </div>
                    <div class="input-group">
                        <label>Никнейм osu!</label>
                        <input type="text" name="osu_identifier" placeholder="Введите ваш ник в игре" required>
                    </div>
                    <div class="info-box">
                        <b>Важно:</b> Вам потребуется скопировать сгенерированный код и вставить его в описание (<b>About Me</b>) вашего профиля osu!
                    </div>
                    <button type="submit" class="btn btn-secondary" style="margin-top:25px;">Продолжить (Классика)</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """

    # Шаблон страницы успешной верификации
    SUCCESS_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <title>Успешная верификация</title>
        <style>
            body {
                margin: 0; padding: 0;
                display: flex; justify-content: center; align-items: center;
                min-height: 100vh;
                color: white; font-family: 'Segoe UI', Roboto, sans-serif;
                background: linear-gradient(135deg, #000 0%, #151515 100%);
            }
            .container {
                text-align: center;
                background: #111;
                padding: 60px;
                border-radius: 24px;
                border: 1px solid #222;
                box-shadow: 0 30px 70px rgba(0,0,0,0.9);
                max-width: 500px;
            }
            .icon {
                font-size: 80px;
                color: #4ade80;
                margin-bottom: 20px;
            }
            .logo { 
                font-size: 48px; 
                font-weight: 900; 
                font-style: italic; 
                margin-bottom: 10px; 
                color: #fff;
            }
            h1 { font-size: 24px; margin-bottom: 15px; }
            p { color: #888; line-height: 1.6; margin-bottom: 30px; }
            .btn {
                display: inline-block;
                padding: 15px 40px;
                background: #fff;
                color: #000;
                text-decoration: none;
                border-radius: 10px;
                font-weight: 800;
                text-transform: uppercase;
                transition: 0.3s;
            }
            .btn:hover { transform: translateY(-3px); filter: brightness(1.1); }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="icon">✓</div>
            <div class="logo">VERIFIED</div>
            <h1>Аккаунт успешно привязан!</h1>
            <p>Ваш профиль osu! успешно соединен с Discord.</p>
            <div style="background:#1a1a1a;border:1px solid #2a2a2a;border-radius:12px;padding:14px;margin:16px 0;text-align:left;">
                <div style="font-size:12px;color:#888;margin-bottom:8px;">Шаг 1: Подтвердите Discord</div>
                <div style="font-family:monospace;font-size:22px;color:#60a5fa;">/linkcode {{LINK_CODE}}</div>
            </div>
            <form method="post" action="/verify/finalize">
                <input type="hidden" name="challenge_id" value="{{CHALLENGE_ID}}" />
                <button class="btn" type="submit">Шаг 2: Завершить и выдать роль</button>
            </form>
        </div>
    </body>
    </html>
    """

    FINALIZE_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <title>Верификация завершена</title>
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                background: #0f0f0f;
                color: #fff;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 20px;
            }
            .container {
                background: #111;
                border: 1px solid #222;
                border-radius: 20px;
                padding: 40px;
                max-width: 520px;
                width: 100%;
                text-align: center;
                box-shadow: 0 10px 40px rgba(0, 0, 0, 0.45);
            }
            .icon {
                font-size: 68px;
                color: #4ade80;
                margin-bottom: 16px;
            }
            h1 { font-size: 34px; margin-bottom: 14px; }
            p { color: #a3a3a3; line-height: 1.6; margin-bottom: 20px; }
            .meta {
                text-align: left;
                background: #151515;
                border: 1px solid #2a2a2a;
                border-radius: 12px;
                padding: 14px;
                font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
                font-size: 14px;
                color: #d4d4d4;
                margin-bottom: 18px;
            }
            .meta div { margin-bottom: 6px; }
            .meta div:last-child { margin-bottom: 0; }
            .hint {
                color: #7dd3fc;
                font-size: 14px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="icon">✓</div>
            <h1>Готово</h1>
            <p>Роль добавлена в очередь. Бот выдаст ее в Discord автоматически.</p>
            <div class="meta">
                <div>mode={{MODE}}</div>
                <div>digit={{DIGIT}}</div>
                <div>role_id={{ROLE_ID}}</div>
            </div>
            <div class="hint">Обычно это занимает несколько секунд.</div>
        </div>
    </body>
    </html>
    """

    @app.get("/", response_class=HTMLResponse)
    async def index(discord_id: str | None = Query(default=None)):
        pref = discord_id if discord_id else ""
        return HTML_TEMPLATE.replace("{{DISCORD_ID}}", pref)

    # ЭТОТ РОУТ НУЖНО ДОБАВИТЬ:
    @app.get("/auth/osu/login")
    async def osu_login(discord_id: str):
        """
        Перенаправляет пользователя на страницу авторизации osu!
        """
        # Формируем state, чтобы прокинуть discord_id в callback
        state = f"discord:{discord_id}"
        
        # Строим URL для авторизации (используем твою функцию из osu_oauth)
        auth_url = build_authorize_url(
            client_id=settings.osu_client_id,
            redirect_uri=settings.osu_redirect_uri,
            state=state
        )
        return RedirectResponse(url=auth_url)

    @app.get("/auth/osu/callback", response_class=HTMLResponse)
    async def osu_callback(code: str, state: str):
        # 1. Извлекаем discord_id из параметра state
        if not state.startswith("discord:"):
            logger.error(f"Invalid state received: {state}")
            raise HTTPException(status_code=400, detail="Invalid state parameter")
        
        discord_id = state.split(":")[1]

        try:
            # 2. Обмениваем временный код на токены
            token_data = await exchange_authorization_code(
                client_id=settings.osu_client_id,
                client_secret=settings.osu_client_secret,
                code=code,
                redirect_uri=settings.osu_redirect_uri
            )
            
            access_token = token_data.get("access_token")
            if not access_token:
                raise HTTPException(status_code=400, detail="Failed to retrieve access token")

            # 3. Запрашиваем данные профиля игрока
            user_data = await fetch_me(access_token)
            osu_user_id = user_data.get("id")
            osu_username = user_data.get("username")

            if not osu_user_id:
                raise HTTPException(status_code=400, detail="Could not fetch osu! user info")

            # 4. Сохраняем связку.
            # Если Supabase SDK не настроен/невалиден, используем прямой SQL через DATABASE_URL.
            saved = False
            sb = get_supabase()
            if sb is not None:
                sb.table("users").upsert(
                    {
                        "discord_id": int(discord_id),
                        "osu_id": int(osu_user_id),
                        "osu_username": osu_username,
                        "verified_at": int(time.time()),
                    }
                ).execute()
                saved = True
            if not saved:
                async with get_db_conn() as conn:
                    await conn.execute(
                        """
                        INSERT INTO users (discord_id, osu_username, osu_id)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (discord_id) DO UPDATE SET
                            osu_username = EXCLUDED.osu_username,
                            osu_id = EXCLUDED.osu_id
                        """,
                        int(discord_id),
                        str(osu_username),
                        int(osu_user_id),
                    )

            mode = str(user_data.get("playmode", "osu"))
            link_code = secrets.token_hex(3).upper()
            now = int(time.time())
            expires = now + settings.verification_token_ttl_seconds
            # verification_challenges in some deployments uses discord_id as PRIMARY KEY.
            # To be backward-compatible, we upsert by discord_id and treat discord_id as challenge_id.
            async with get_db_conn() as conn:
                await conn.execute(
                    """
                    INSERT INTO verification_challenges
                    (discord_id, osu_id, osu_username, mode, profile_token, status, created_at, expires_at, verification_source, link_code)
                    VALUES ($1, $2, $3, $4, $5, 'pending', $6, $7, 'oauth', $8)
                    ON CONFLICT (discord_id) DO UPDATE SET
                        osu_id=EXCLUDED.osu_id,
                        osu_username=EXCLUDED.osu_username,
                        mode=EXCLUDED.mode,
                        profile_token=EXCLUDED.profile_token,
                        status=EXCLUDED.status,
                        created_at=EXCLUDED.created_at,
                        expires_at=EXCLUDED.expires_at,
                        verification_source=EXCLUDED.verification_source,
                        link_code=EXCLUDED.link_code
                    """,
                    int(discord_id),
                    int(osu_user_id),
                    str(osu_username),
                    mode,
                    "oauth",
                    now,
                    expires,
                    link_code,
                )
            challenge_id = int(discord_id)

            logger.info(f"Success! Linked Discord:{discord_id} to osu!:{osu_username} challenge={challenge_id}")

            # 5. Возвращаем UI с ником игрока
            final_html = SUCCESS_TEMPLATE.replace(
                "<h1>Аккаунт успешно привязан!</h1>", 
                f"<h1>{osu_username}, аккаунт привязан!</h1>"
            )
            final_html = final_html.replace("{{LINK_CODE}}", link_code).replace("{{CHALLENGE_ID}}", str(challenge_id))
            return HTMLResponse(content=final_html)

        except Exception as e:
            logger.exception("Error during osu! verification process")
            return HTMLResponse(content="<h1>Ошибка верификации</h1><p>Попробуйте позже.</p>", status_code=500)

    # --- Вариант для ручной проверки (classic) ---
    @app.post("/verify/classic/start")
    async def classic_verify(discord_id: str = Form(...), osu_identifier: str = Form(...)):
        # Здесь будет твоя логика для Способа 2
        return {"status": "in_progress", "message": "Logic not implemented yet"}

    @app.post("/verify/finalize")
    async def finalize_verification(challenge_id: int = Form(...)):
        now = int(time.time())
        async with get_db_conn() as conn:
            row = await conn.fetchrow(
                """
                SELECT discord_id, osu_id, osu_username, mode, status, expires_at
                FROM verification_challenges
                WHERE discord_id=$1
                """,
                int(challenge_id),
            )
            if not row:
                raise HTTPException(status_code=404, detail="Challenge not found")
            if row["status"] != "pending":
                raise HTTPException(status_code=400, detail="Challenge already processed")
            if now > int(row["expires_at"]):
                raise HTTPException(status_code=410, detail="Challenge expired")
            verified = await conn.fetchrow(
                "SELECT 1 FROM verified_discord_links WHERE discord_id=$1",
                int(row["discord_id"]),
            )
            if not verified:
                raise HTTPException(status_code=403, detail="Сначала выполни /linkcode в Discord")

        osu_user = await osu_client.request(f"users/{int(row['osu_id'])}")
        if not osu_user:
            raise HTTPException(status_code=502, detail="osu API unavailable")
        global_rank = (osu_user.get("statistics") or {}).get("global_rank")
        digit = compute_digit_value(
            VerificationInput(
                osu_id=int(row["osu_id"]),
                username=str(row["osu_username"]),
                global_rank=global_rank,
            ),
            settings.verification_mode,
            digit_modulus=settings.digit_modulus,
        )
        role_id = role_mapping.get(str(row["mode"]), {}).get(digit)
        if not role_id:
            raise HTTPException(status_code=400, detail=f"No role for mode={row['mode']} digit={digit}")

        async with get_db_conn() as conn:
            await conn.execute(
                """
                INSERT INTO pending_role_assignments
                (discord_id, osu_id, osu_username, mode, digit_value, role_id, status, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, 'pending', $7)
                """,
                int(row["discord_id"]),
                int(row["osu_id"]),
                str(row["osu_username"]),
                str(row["mode"]),
                int(digit),
                int(role_id),
                now,
            )
            await conn.execute(
                "UPDATE verification_challenges SET status='done' WHERE discord_id=$1",
                int(row["discord_id"]),
            )
        done_html = (
            FINALIZE_TEMPLATE
            .replace("{{MODE}}", str(row["mode"]))
            .replace("{{DIGIT}}", str(digit))
            .replace("{{ROLE_ID}}", str(role_id))
        )
        return HTMLResponse(content=done_html, status_code=200)

    @app.get("/debug_verify")
    async def debug_verify(osu_identifier: str):
        """
        Debug endpoint: check mode/digit/role resolution without changing DB state.
        """
        user = await osu_client.request(f"users/{osu_identifier}")
        if not user:
            raise HTTPException(status_code=404, detail="osu user not found")
        mode = str(user.get("playmode", "osu"))
        osu_id = int(user["id"])
        username = str(user["username"])
        global_rank = (user.get("statistics") or {}).get("global_rank")
        try:
            digit = compute_digit_value(
                VerificationInput(
                    osu_id=osu_id,
                    username=username,
                    global_rank=global_rank,
                ),
                settings.verification_mode,
                digit_modulus=settings.digit_modulus,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"digit compute failed: {exc}") from exc

        mode_map = role_mapping.get(mode, {})
        role_id = mode_map.get(digit)
        return {
            "ok": True,
            "osu_id": osu_id,
            "username": username,
            "mode": mode,
            "verification_mode": settings.verification_mode,
            "global_rank": global_rank,
            "digit": digit,
            "role_id": role_id,
            "has_role_mapping_for_mode": bool(mode_map),
            "available_digits_for_mode": sorted(mode_map.keys()),
        }

    # ГЛАВНОЕ: return app находится ВНЕ функций роутов, но ВНУТРИ create_web_app
    return app