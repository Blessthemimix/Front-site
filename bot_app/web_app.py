from __future__ import annotations
import logging
import secrets
import time
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from .config import Settings
from .osu_client import OsuClient
from .osu_oauth import build_authorize_url, exchange_authorization_code, fetch_me
from .db import get_db_conn

logger = logging.getLogger(__name__)

def create_web_app(
    *,
    settings: Settings,
    osu_client: OsuClient,
    role_mapping: dict[str, dict[int, int]],
) -> FastAPI:
    app = FastAPI()

    # --- HTML ШАБЛОН С ДВУМЯ СПОСОБАМИ ---
    def get_main_page(discord_id_val: str = ""):
        return f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>osu! Verify</title>
            <style>
                body {{
                    background-color: #0a0a0a;
                    color: white;
                    font-family: 'Inter', -apple-system, sans-serif;
                    display: flex;
                    justify-content: center;
                    align-items: center;
                    height: 100vh;
                    margin: 0;
                    background-image: radial-gradient(circle at center, #111 0%, #000 100%);
                }}
                .container {{
                    display: flex;
                    width: 900px;
                    height: 600px;
                    background: #111;
                    border-radius: 24px;
                    overflow: hidden;
                    border: 1px solid #222;
                    box-shadow: 0 20px 50px rgba(0,0,0,0.8);
                }}
                .left-panel {{
                    flex: 1;
                    padding: 60px;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    background: linear-gradient(135deg, #111 0%, #161616 100%);
                    border-right: 1px solid #222;
                }}
                .right-panel {{
                    flex: 1.2;
                    padding: 40px;
                    overflow-y: auto;
                    background: #111;
                }}
                .logo {{ font-size: 48px; font-weight: 900; italic; margin-bottom: 20px; letter-spacing: -2px; }}
                .status {{ font-size: 12px; color: #4ade80; display: flex; align-items: center; gap: 8px; text-transform: uppercase; }}
                .status::before {{ content: ''; width: 8px; height: 8px; background: #4ade80; border-radius: 50%; }}
                
                h3 {{ font-size: 10px; color: #666; text-transform: uppercase; margin-bottom: 15px; letter-spacing: 1px; }}
                .input-group {{ margin-bottom: 20px; }}
                label {{ display: block; font-size: 11px; color: #888; margin-bottom: 8px; text-transform: uppercase; }}
                input {{
                    width: 100%;
                    background: #1a1a1a;
                    border: 1px solid #333;
                    padding: 12px;
                    border-radius: 8px;
                    color: white;
                    box-sizing: border-box;
                    outline: none;
                    transition: border 0.2s;
                }}
                input:focus {{ border-color: #555; }}
                
                .btn {{
                    width: 100%;
                    padding: 14px;
                    border-radius: 8px;
                    font-weight: bold;
                    cursor: pointer;
                    border: none;
                    text-transform: uppercase;
                    transition: 0.3s;
                    text-decoration: none;
                    display: inline-block;
                    text-align: center;
                }}
                .btn-primary {{ background: white; color: black; }}
                .btn-secondary {{ background: #222; color: #888; }}
                .btn:hover {{ transform: translateY(-2px); filter: brightness(0.9); }}
                
                .divider {{ height: 1px; background: #222; margin: 30px 0; position: relative; }}
                .info-box {{ background: #161625; padding: 12px; border-radius: 8px; font-size: 11px; color: #78789d; border: 1px solid #232342; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="left-panel">
                    <div class="logo">VERIFY</div>
                    <p style="color: #888; line-height: 1.6;">Свяжите свои аккаунты Discord и <b>osu!</b> для автоматического получения ролей.</p>
                    <div class="status">System Online</div>
                </div>
                <div class="right-panel">
                    <h3>Способ 1 — osu! OAuth</h3>
                    <form action="/auth/osu/login" method="get">
                        <div class="input-group">
                            <label>Discord ID</label>
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Напр: 123456789..." required>
                        </div>
                        <button type="submit" class="btn btn-primary">Войти через osu!</button>
                    </form>

                    <div class="divider"></div>

                    <h3>Способ 2 — Ник и Токен в Bio</h3>
                    <form action="/verify/classic/start" method="post">
                        <div class="input-group">
                            <label>Discord ID</label>
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Напр: 123456789..." required>
                        </div>
                        <div class="input-group">
                            <label>osu! Username / URL</label>
                            <input type="text" name="osu_identifier" placeholder="Ник или ссылка" required>
                        </div>
                        <div class="info-box">
                            После нажатия кнопки вы получите токен, который нужно вставить в <b>About Me</b> на osu!
                        </div>
                        <button type="submit" class="btn btn-secondary" style="margin-top:20px;">Дальше (Классика)</button>
                    </form>
                </div>
            </div>
        </body>
        </html>
        """

    @app.get("/", response_class=HTMLResponse)
    async def index(discord_id: str | None = Query(default=None)):
        return get_main_page(discord_id or "")

    # --- ЛОГИКА СПОСОБА 1 (OAUTH) ---
    @app.get("/auth/osu/login")
    async def osu_oauth_login(discord_id: int):
        state = secrets.token_urlsafe(32)
        conn = await get_db_conn()
        try:
            await conn.execute(
                "INSERT INTO oauth_osu_states (state, discord_id, created_at, expires_at) VALUES ($1, $2, $3, $4)",
                state, discord_id, int(time.time()), int(time.time()) + 600
            )
        finally: await conn.close()
        
        url = build_authorize_url(str(settings.osu_client_id), settings.osu_redirect_uri, state, "public")
        return RedirectResponse(url=url)

    @app.get("/auth/osu/callback")
    async def osu_oauth_callback(code: str, state: str):
        conn = await get_db_conn()
        try:
            row = await conn.fetchrow("SELECT discord_id FROM oauth_osu_states WHERE state=$1", state)
            if not row: raise HTTPException(400, "Invalid State")
            
            tokens = await exchange_authorization_code(str(settings.osu_client_id), str(settings.osu_client_secret), code, settings.osu_redirect_uri)
            me = await fetch_me(tokens["access_token"])
            
            # Сразу финализируем или создаем запись
            # Здесь логика как в твоем предыдущем скрипте (создание challenge и т.д.)
            return HTMLResponse(f"Успешно авторизован как {me['username']}")
        finally: await conn.close()

    # --- ЛОГИКА СПОСОБА 2 (КЛАССИКА) ---
    @app.post("/verify/classic/start")
    async def classic_start(discord_id: int = Form(...), osu_identifier: str = Form(...)):
        # Здесь логика генерации токена (profile_token)
        # И возврат страницы, где написано "Вставьте этот токен в профиль"
        return HTMLResponse(f"Генерируем токен для {osu_identifier}...")

    return app