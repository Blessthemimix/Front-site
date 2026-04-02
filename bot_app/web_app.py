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
import os
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

logger = logging.getLogger(__name__)

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
            <p>Ваш профиль osu! успешно соединен с Discord. Роли будут выданы автоматически в течение нескольких минут.</p>
            <a href="https://discord.com/app" class="btn">Вернуться в Discord</a>
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

            # 4. Сохраняем связку в базу данных Supabase
            async with get_db_conn(settings.supabase_url, settings.supabase_key) as db:
                await db.table("users").upsert({
                    "discord_id": discord_id,
                    "osu_id": str(osu_user_id),
                    "osu_username": osu_username,
                    "verified_at": int(time.time())
                }).execute()

            logger.info(f"Success! Linked Discord:{discord_id} to osu!:{osu_username}")

            # 5. Возвращаем UI с ником игрока
            final_html = SUCCESS_TEMPLATE.replace(
                "<h1>Аккаунт успешно привязан!</h1>", 
                f"<h1>{osu_username}, аккаунт привязан!</h1>"
            )
            return HTMLResponse(content=final_html)

        except Exception as e:
            logger.exception("Error during osu! verification process")
            return HTMLResponse(content="<h1>Ошибка верификации</h1><p>Попробуйте позже.</p>", status_code=500)

    # --- Вариант для ручной проверки (classic) ---
    @app.post("/verify/classic/start")
    async def classic_verify(discord_id: str = Form(...), osu_identifier: str = Form(...)):
        # Здесь будет твоя логика для Способа 2
        return {"status": "in_progress", "message": "Logic not implemented yet"}

    # ГЛАВНОЕ: return app находится ВНЕ функций роутов, но ВНУТРИ create_web_app
    return app