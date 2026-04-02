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

logger = logging.getLogger(__name__)

def create_web_app(*, settings: Settings, osu_client: OsuClient, role_mapping: dict[str, dict[int, int]]) -> FastAPI:
    app = FastAPI()

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
                    /* ГРАДИЕНТ ЧЕРНЫЙ - ТЕМНО-СЕРЫЙ + СЕТКА */
                    background: linear-gradient(135deg, #000 0%, #151515 100%);
                    position: relative;
                }}
                body::before {{
                    content: "";
                    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                    background-image: linear-gradient(0deg, transparent 24%, rgba(255, 255, 255, .02) 25%, rgba(255, 255, 255, .02) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .02) 75%, rgba(255, 255, 255, .02) 76%, transparent 77%, transparent), 
                                     linear-gradient(90deg, transparent 24%, rgba(255, 255, 255, .02) 25%, rgba(255, 255, 255, .02) 26%, transparent 27%, transparent 74%, rgba(255, 255, 255, .02) 75%, rgba(255, 255, 255, .02) 76%, transparent 77%, transparent);
                    background-size: 60px 60px;
                    z-index: -1;
                }}
                .container {{
                    display: flex;
                    width: 980px; height: 640px;
                    background: #111;
                    border-radius: 24px;
                    overflow: hidden;
                    border: 1px solid #222;
                    box-shadow: 0 30px 70px rgba(0,0,0,0.9);
                }}
                .left-panel {{
                    flex: 1;
                    padding: 60px;
                    display: flex;
                    flex-direction: column;
                    justify-content: center;
                    background: #0d0d0d; /* Темнее основного фона */
                    border-right: 1px solid #222;
                }}
                .right-panel {{
                    flex: 1.3;
                    padding: 45px 55px;
                    overflow-y: auto;
                    background: #111;
                }}
                /* УВЕЛИЧЕННЫЙ ЛОГОТИП VERIFY */
                .logo {{ 
                    font-size: 64px; 
                    font-weight: 900; 
                    font-style: italic; 
                    margin-bottom: 25px; 
                    letter-spacing: -1px; 
                    color: #fff;
                    line-height: 1;
                }}
                .status {{ font-size: 13px; color: #4ade80; display: flex; align-items: center; gap: 10px; text-transform: uppercase; font-weight: 700; }}
                .status::before {{ content: ''; width: 10px; height: 10px; background: #4ade80; border-radius: 50%; box-shadow: 0 0 10px #4ade80; }}
                
                h3 {{ font-size: 11px; color: #666; text-transform: uppercase; margin: 35px 0 20px 0; letter-spacing: 1.5px; font-weight: 700; }}
                h3:first-child {{ margin-top: 0; }}
                
                .input-group {{ margin-bottom: 25px; }}
                label {{ display: block; font-size: 12px; color: #888; margin-bottom: 10px; text-transform: uppercase; font-weight: 700; }}
                .label-help {{ font-size: 10px; color: #555; text-transform: none; margin-left: 5px; font-weight: 400; display: block; margin-top: 4px; }}
                
                input {{
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
                }}
                input:focus {{ border-color: #555; background: #1c1c1c; box-shadow: 0 0 15px rgba(255,255,255,0.02); }}
                
                .btn {{
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
                }}
                .btn-primary {{ background: #fff; color: #000; }}
                .btn-secondary {{ background: #1e1e1e; color: #999; border: 1px solid #333; }}
                .btn:hover {{ transform: translateY(-3px); filter: brightness(1.1); box-shadow: 0 10px 20px rgba(0,0,0,0.4); }}
                
                .divider {{ height: 1px; background: #222; margin: 35px 0; position: relative; }}
                .info-box {{ background: #141420; padding: 18px; border-radius: 10px; font-size: 12px; color: #7a7a9e; border: 1px solid #1e1e35; line-height: 1.6; }}
                b {{ color: #eee; font-weight: 700; }}
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
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Напр: 1160688626934497481" required>
                        </div>
                        <button type="submit" class="btn btn-primary">Войти через osu!</button>
                    </form>

                    <div class="divider"></div>

                    <h3>Способ 2 — Ручная проверка</h3>
                    <form action="/verify/classic/start" method="post">
                        <div class="input-group">
                            <label>Discord ID</label>
                            <input type="text" name="discord_id" value="{discord_id_val}" placeholder="Ваш 18-значный ID" required>
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

    @app.get("/", response_class=HTMLResponse)
    async def index(discord_id: str | None = Query(default=None)):
        return get_main_page(discord_id or "")

    return app