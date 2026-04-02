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

    # --- HTML ШАБЛОН С ДВУМЯ СПОСОБАМИ (УЛУЧШЕННЫЙ ДИЗАЙН) ---
    def get_main_page(discord_id_val: str = ""):
        return f"""
        <!DOCTYPE html>
        <html lang="ru">
        <head>
            <meta charset="UTF-8">
            <title>osu! Verify</title>
            <style>
                body {{
                    margin: 0; padding: 0;
                    display: flex; justify-content: center; align-items: center;
                    min-height: 100vh;
                    color: white; font-family: 'Segoe UI', Roboto, sans-serif;
                    /* Задний фон: темный градиент + тонкая сетка */
                    background-color: #080808;
                    background-image: linear-gradient(0deg, transparent 24%, rgba(255, 255, 255, .03) 25%, rgba(255, 255, 255, .03) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .03) 75%, rgba(255, 255, 255, .03) 76%, transparent 77%, transparent), linear-gradient(90deg, transparent 24%, rgba(255, 255, 255, .03) 25%, rgba(255, 255, 255, .03) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .03) 75%, rgba(255, 255, 255, .03) 76%, transparent 77%, transparent);
                    background-size: 50px 50px;
                }}
                .container {{
                    display: flex;
                    width: 960px; height: 620px;
                    background: #111; /* Темно-серый фон правой панели */
                    border-radius: 20px;
                    overflow: hidden;
                    border: 1px solid #222;
                    box-shadow: 0 25px 60px rgba(0,0,0,0.9);
                }}
                .left-panel {{
                    flex: 1;
                    padding: 60px;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    background: #111; /* ЕЩЕ ТЕМНЕЕ ФОН ЛЕВОЙ ПАНЕЛИ */
                    border-right: 1px solid #222;
                }}
                .right-panel {{
                    flex: 1.3;
                    padding: 40px 50px;
                    overflow-y: auto;
                    background: #111; /* Фон правой панели */
                }}
                /* Лого: Bold + Italic */
                .logo {{ font-size: 52px; font-weight: 900; font-style: italic; margin-bottom: 20px; letter-spacing: -2px; color: #fff; }}
                .status {{ font-size: 13px; color: #4ade80; display: flex; align-items: center; gap: 8px; text-transform: uppercase; font-weight: 600; }}
                .status::before {{ content: ''; width: 9px; height: 9px; background: #4ade80; border-radius: 50%; display: inline-block; }}
                
                h3 {{ font-size: 11px; color: #777; text-transform: uppercase; margin: 35px 0 18px 0; letter-spacing: 1.2px; font-weight: 600; }}
                h3:first-child {{ margin-top: 0; }}
                
                .input-group {{ margin-bottom: 22px; }}
                label {{ display: block; font-size: 12px; color: #999; margin-bottom: 9px; text-transform: uppercase; font-weight: 600; }}
                /* Описание как получить Discord ID */
                .label-help {{ font-size: 10px; color: #666; text-transform: none; margin-left: 5px; font-weight: 400; }}
                
                input {{
                    width: 100%;
                    background: #181818;
                    border: 1px solid #333;
                    padding: 14px;
                    border-radius: 8px;
                    color: white;
                    box-sizing: border-box;
                    outline: none;
                    transition: border 0.2s, background 0.2s;
                    font-size: 14px;
                }}
                input:focus {{ border-color: #666; background: #1a1a1a; }}
                
                .btn {{
                    width: 100%;
                    padding: 16px;
                    border-radius: 8px;
                    font-weight: 700;
                    cursor: pointer;
                    border: none;
                    text-transform: uppercase;
                    transition: 0.3s;
                    text-decoration: none;
                    display: inline-block;
                    text-align: center;
                    font-size: 14px;
                }}
                .btn-primary {{ background: white; color: black; }}
                .btn-secondary {{ background: #222; color: #999; border: 1px solid #333; }}
                .btn:hover {{ transform: translateY(-2px); filter: brightness(0.95); }}
                
                .divider {{ height: 1px; background: #222; margin: 30px 0; position: relative; }}
                .info-box {{ background: #161625; padding: 15px; border-radius: 8px; font-size: 12px; color: #8888b5; border: 1px solid #232342; line-height: 1.5; }}
                .info-box b {{ color: #a0a0ff; }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="left-panel">
                    <div class="logo">VERIFY</div>
                    <p style="color: #999; line-height: 1.6; font-size: 15px; margin-bottom: 30px;">Свяжите свои аккаунты Discord и <b>osu!</b> для автоматического получения ролей.</p>
                    <div class="status">System Online</div>
                </div>
                <div class="right-panel">
                    <h3>Способ 1 — osu! OAuth (Быстрый)</h3>
                    <form action="/auth/osu/login" method="get">
                        <div class="input-group">
                            <label>Discord ID <span class="label-help">(Настройки → Расширенные → Режим разработчика (ВКЛ) → ПКМ по аватару → Скопировать ID)</span></label>
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Напр: 1160688626934497481" required>
                        </div>
                        <button type="submit" class="btn btn-primary">Войти через osu!</button>
                    </form>

                    <div class="divider"></div>

                    <h3>Способ 2 — Токен в Описании профиля</h3>
                    <form action="/verify/classic/start" method="post">
                        <div class="input-group">
                            <label>Discord ID <span class="label-help">(Настройки → Расширенные → Режим разработчика (ВКЛ) → ПКМ по аватару → Скопировать ID)</span></label>
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Напр: 1160688626934497481" required>
                        </div>
                        <div class="input-group">
                            <label>osu! Username / URL профиля</label>
                            <input type="text" name="osu_identifier" placeholder="Никнейм или ссылка на профиль" required>
                        </div>
                        <div class="info-box">
                            На следующем шаге вы получите токен, который нужно будет вставить в раздел <b>About Me</b> (Обо мне) на сайте osu!
                        </div>
                        <button type="submit" class="btn btn-secondary" style="margin-top:25px;">Дальше (Классика)</button>
                    </form>
                </div>
            </div>
        </body>
        </html>
        """

    @app.get("/", response_class=HTMLResponse)
    async def index(discord_id: str | None = Query(default=None)):
        return get_main_page(discord_id or "")

    # Остальная логика роутов (OAuth, Classic) должна быть ниже, без изменений...
    return app