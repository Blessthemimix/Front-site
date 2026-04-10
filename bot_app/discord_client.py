"""Discord bot implementation and role assignment worker."""

from __future__ import annotations
from datetime import datetime, timezone
from collections import defaultdict
import asyncio
import logging
import random
import time
from io import BytesIO, StringIO
from typing import Any
import aiohttp
from PIL import Image, ImageDraw, ImageFont, ImageFilter

import discord
from colorthief import ColorThief
from discord.ext import commands, tasks
from discord import app_commands

from .config import Settings
from .db import get_db_conn
from .osu_client import OsuClient

logger = logging.getLogger(__name__)

# --- Вспомогательные функции ---

def _parse_username_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw: return []
    if "," in raw or ";" in raw or "\n" in raw:
        parts = []
        for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
            c = chunk.strip()
            if c: parts.append(c)
        return parts
    return [p for p in raw.split() if p]

def get_all_digit_role_ids(role_mapping: dict[str, dict[int, int]]) -> set[int]:
    return {role_id for mode_map in role_mapping.values() for role_id in mode_map.values()}

def _ruleset_id_from_beatmap(bm: dict[str, Any]) -> int:
    mi = bm.get("mode_int")
    if mi is not None: return int(mi)
    return {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}.get(str(bm.get("mode") or "osu"), 0)

async def _get_map_top_pp(bot, beatmap_id: int) -> float:
    """Берёт PP топ-1 с глобального leaderboard карты"""
    try:
        data = await bot.osu_client.request(f"beatmaps/{beatmap_id}/scores?limit=1")
        
        if not data or not data.get("scores"):
            return 0.0
        
        top_score = data["scores"][0]
        return float(top_score.get("pp") or 0.0)
    
    except Exception:
        return 0.0

async def _star_rating_for_score(bot, score: dict[str, Any]) -> float:
    bm = score.get("beatmap") or {}
    mods = score.get("mods") or []
    has_sr_mods = any(m.get('acronym') in ('DT', 'NC', 'HT', 'DC', 'HR', 'EZ') for m in mods if isinstance(m, dict))
    if not has_sr_mods:
        return float(bm.get("difficulty_rating") or 0)
    bid = bm.get("id")
    if not bid: return float(bm.get("difficulty_rating") or 0)
    got = await bot.osu_client.beatmap_star_rating(int(bid), mods, _ruleset_id_from_beatmap(bm))
    return got if got is not None else float(bm.get("difficulty_rating") or 0)

def _infer_pattern_label(bm: dict[str, Any]) -> str:
    circles = int(bm.get("count_circles") or 0)
    sliders = int(bm.get("count_sliders") or 0)
    objects = circles + sliders
    slider_ratio = sliders / max(objects, 1)
    bpm = float(bm.get("bpm") or 0)

    if slider_ratio > 0.61: label = "Full LN"
    elif slider_ratio >= 0.35: label = "Heavy LN"
    elif slider_ratio >= 0.18: label = "Hybrid"
    else: label = "Rice"

    if bpm >= 270: return f"{label} / Speed"
    return label

# --- ГЕНЕРАЦИЯ КАРТОЧКИ ---

def _get_theme_data_sync(image_data: bytes) -> dict:
    """Анализирует палитру и возвращает настройки темы (цвета фона и текста)."""
    try:
        cf = ColorThief(BytesIO(image_data))
        palette = cf.get_palette(color_count=5, quality=1)
        r, g, b = palette[0] # Доминирующий цвет
        
        # Считаем яркость (Luminance)
        luminance = (0.299 * r + 0.587 * g + 0.114 * b)
        is_light_theme = luminance > 160 # Порог яркости

        # Базовый акцентный цвет (чуть сочнее)
        h, l, s = colorsys.rgb_to_hls(r/255.0, g/255.0, b/255.0)
        
        if is_light_theme:
            # Светлая тема: Белый фон, темный текст, насыщенный акцент
            l_adj = min(0.4, l) # Делаем цвет темнее для читаемости на белом
            s_adj = max(0.7, s)
            accent_rgb = colorsys.hls_to_rgb(h, l_adj, s_adj)
            
            return {
                "is_light": True,
                "bg": (255, 255, 255, 255),
                "main_text": (40, 40, 45, 255),
                "sub_text": (80, 80, 90, 255),
                "accent": (int(accent_rgb[0]*255), int(accent_rgb[1]*255), int(accent_rgb[2]*255), 255),
                "bar_bg": (230, 230, 235, 255)
            }
        else:
            # Темная тема: Темный фон, белый текст, яркий акцент
            l_adj = max(0.6, l) # Делаем цвет светлее для контраста на темном
            s_adj = max(0.6, s)
            accent_rgb = colorsys.hls_to_rgb(h, l_adj, s_adj)
            
            return {
                "is_light": False,
                "bg": (20, 22, 25, 255),
                "main_text": (255, 255, 255, 255),
                "sub_text": (180, 180, 190, 255),
                "accent": (int(accent_rgb[0]*255), int(accent_rgb[1]*255), int(accent_rgb[2]*255), 255),
                "bar_bg": (45, 48, 52, 255)
            }
    except:
        # Дефолтная темная тема
        return {
            "is_light": False, "bg": (20,20,25,255), "main_text": (255,255,255,255),
            "sub_text": (180,180,180,255), "accent": (255, 198, 0, 255), "bar_bg": (40,40,45,255)
        }

async def generate_map_card(bot, bm_data: dict, pp_values: dict):
    W, H = 1000, 520
    
    async with aiohttp.ClientSession() as session:
        async def fetch_img(url):
            async with session.get(url) as resp:
                return await resp.read() if resp.status == 200 else None

        cover_url = bm_data["beatmapset"]["covers"]["cover@2x"]
        avatar_url = f"https://a.ppy.sh/{bm_data['beatmapset']['user_id']}"
        cover_data, avatar_data = await asyncio.gather(fetch_img(cover_url), fetch_img(avatar_url))

    theme = await asyncio.to_thread(_get_theme_data_sync, cover_data) if cover_data else _get_theme_data_sync(b"")

    card = Image.new('RGBA', (W, H), theme["bg"])
    draw = ImageDraw.Draw(card)

    # --- 1. Баннер сверху ---
    if cover_data:
        bg_img = Image.open(BytesIO(cover_data)).convert("RGBA")
        bw, bh = bg_img.size
        ratio = W / bw
        bg_img = bg_img.resize((W, int(bh * ratio)), Image.Resampling.LANCZOS)
        card.paste(bg_img.crop((0, 0, W, 240)), (0, 0))

    # --- Шрифты (уменьшаем f_info для лучшей читаемости) ---
    try:
        f_title = ImageFont.truetype("bot_app/Roboto-Bold.ttf", 36)
        f_sub = ImageFont.truetype("bot_app/Roboto-Regular.ttf", 22)
        f_stat = ImageFont.truetype("bot_app/Roboto-Bold.ttf", 20)
        # Уменьшил с 24 до 20, чтобы текст стал изящнее
        f_info = ImageFont.truetype("bot_app/Roboto-Bold.ttf", 20) 
        f_stars = ImageFont.truetype("bot_app/Segoe UI Unicode Regular.otf", 24) 
    except Exception as e:
        f_title = f_sub = f_stars = f_stat = f_info = ImageFont.load_default()

    # --- 2. Контент (Исправленный блок) ---
    text_y = 265
    
    # Название карты
    draw.text((45, text_y), bm_data["beatmapset"]["title"][:55], font=f_title, fill=theme["accent"])
    
    # Исполнитель и сложность
    draw.text((45, text_y + 45), f"{bm_data['beatmapset']['artist']} // [{bm_data['version']}]", font=f_sub, fill=theme["sub_text"])
    
    # Рейтинг сложности (Символы + Число)
    sr = bm_data["difficulty_rating"]
    icon = "⯌"  # Твой новый символ
    rating_icons = icon * min(int(sr), 10)
    draw.text((45, text_y + 80), f"{rating_icons} {sr:.2f}", font=f_stars, fill=theme["accent"])

    # --- 3. ИНФО-БЛОК (BPM, Length, Notes) ---
    
    # Расчеты
    total_sec = bm_data.get("total_length", 0)
    mins, secs = divmod(total_sec, 60)
    time_str = f"{mins:02d}:{secs:02d}"
    
    total_objects = (int(bm_data.get("count_circles") or 0) + 
                     int(bm_data.get("count_sliders") or 0) + 
                     int(bm_data.get("count_spinners") or 0))
    bpm_val = int(bm_data.get("bpm") or 0)
    
    # Координаты
    info_y = text_y + 55 
    
    # Фиксируем позицию Length и Notes (как на скриншотах, где они были справа)
    # А BPM рисуем с небольшим отступом СЛЕВА от Length
    length_x = 730 
    notes_x = 860
    bpm_x = length_x - 95 # Подтягиваем BPM вплотную к Length
    
    # Отрисовка
    # BPM (теперь он близко к Length)
    draw.text((bpm_x, info_y), f"BPM: {bpm_val}", font=f_info, fill=theme["main_text"])
    
    # Length (остался на месте)
    draw.text((length_x, info_y), f"Length: {time_str}", font=f_info, fill=theme["main_text"])
    
    # Notes (остался на месте)
    draw.text((notes_x, info_y), f"Notes: {total_objects}", font=f_info, fill=theme["main_text"])

    # PP Блок (Справа)
    pp_x = 720
    pp_y = text_y + 110
    for acc in ["95", "98", "99", "100" ]:
        draw.text((pp_x, pp_y), f"{acc}%:", font=f_stat, fill=theme["accent"])
        val = pp_values.get(acc, 0)
        draw.text((pp_x + 50, pp_y), f"{val:.2f}pp", font=f_stat, fill=theme["main_text"])
        pp_y += 35


    # --- Отрисовка полосок статов (CS, AR, HP, OD) слева ---
    stats_y = text_y + 130 
    for i, (label, val) in enumerate([("CS", bm_data["cs"]), ("AR", bm_data["ar"]), 
                                      ("HP", bm_data["drain"]), ("OD", bm_data["accuracy"])]):
        y = stats_y + (i * 40)
        draw.text((45, y), label, font=f_stat, fill=theme["accent"])
        
        bar_x_start = 100
        bar_width = 200
        draw.rounded_rectangle([bar_x_start, y+8, bar_x_start + bar_width, y+18], radius=5, fill=theme["bar_bg"])
        
        fill_w = int(bar_width * (min(val, 10) / 10))
        if fill_w > 0:
            draw.rounded_rectangle([bar_x_start, y+8, bar_x_start + fill_w, y+18], radius=5, fill=theme["accent"])
        draw.text((bar_x_start + bar_width + 15, y), f"{val:.1f}", font=f_stat, fill=theme["main_text"])

    # --- 4. Аватар маппера ---
    if avatar_data:
        av = Image.open(BytesIO(avatar_data)).convert("RGBA").resize((80, 80), Image.Resampling.LANCZOS)
        mask = Image.new('L', (80, 80), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0,0,80,80], radius=15, fill=255)
        card.paste(av, (480, 380), mask)
        draw.text((520, 470), bm_data['beatmapset']['creator'], font=f_sub, fill=theme["main_text"], anchor="mt")

    # Скругление всей карточки
    final_mask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(final_mask).rounded_rectangle([0, 0, W, H], radius=30, fill=255)
    output = Image.new('RGBA', (W, H), (0,0,0,0))
    output.paste(card, (0,0), final_mask)

    buf = BytesIO()
    output.save(buf, format='PNG')
    buf.seek(0)
    return buf

# --- КЛАСС БОТА ---

class RoleBot(commands.Bot):
    def __init__(self, *, settings: Settings, osu_client: OsuClient, role_mapping: dict[str, dict[int, int]]) -> None:
        intents = discord.Intents.all()
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.osu_client = osu_client
        self.role_mapping = role_mapping

    async def setup_hook(self) -> None:
        async with get_db_conn() as db_conn:
            await db_conn.execute("""
                CREATE TABLE IF NOT EXISTS scraped_beatmaps (
                    beatmap_id BIGINT PRIMARY KEY,
                    title TEXT,
                    pp_max FLOAT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
        self.poll_pending_assignments.start()
        await self.tree.sync()

    @tasks.loop(seconds=5.0)
    async def poll_pending_assignments(self) -> None:
        guild = self.get_guild(self.settings.discord_guild_id)
        if not guild: return
        async with get_db_conn() as db_conn:
            rows = await db_conn.fetch("SELECT id, discord_id, role_id FROM pending_role_assignments WHERE status='pending' LIMIT 20")
            for r in rows:
                try:
                    member = guild.get_member(r["discord_id"]) or await guild.fetch_member(r["discord_id"])
                    role = guild.get_role(r["role_id"])
                    if member and role:
                        await self._replace_digit_roles(member, role)
                        await db_conn.execute("UPDATE pending_role_assignments SET status='done' WHERE id=$1", r["id"])
                except Exception as e:
                    logger.error(f"Role error: {e}")

    async def _replace_digit_roles(self, member: discord.Member, target_role: discord.Role) -> None:
        role_ids = get_all_digit_role_ids(self.role_mapping)
        old = [r for r in member.roles if r.id in role_ids]
        if old: await member.remove_roles(*old)
        await member.add_roles(target_role)

# --- РЕГИСТРАЦИЯ КОМАНД ---

def register_commands(bot: RoleBot) -> None:
    @bot.tree.command(name="recommend", description="Список 4 карт + карточка первой")
    async def recommend(interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id, osu_username FROM users WHERE discord_id=$1", int(interaction.user.id))
        
        if not row:
            return await interaction.followup.send("Сначала привяжите профиль.")

        best = await bot.osu_client.request(f"users/{row['osu_id']}/scores/best?limit=50")
        if not best: 
            return await interaction.followup.send("Не удалось получить данные профиля.")

        # Фильтруем только Mania (ruleset 3)
        all_srs = [await _star_rating_for_score(bot, s) for s in best if _ruleset_id_from_beatmap(s.get('beatmap', {})) == 3]
        if not all_srs: 
            return await interaction.followup.send("Недостаточно данных в режиме Mania.")
        
        all_srs.sort()
        median_sr = all_srs[len(all_srs)//2]

        async with get_db_conn() as db_conn:
            scraped = await db_conn.fetch("SELECT beatmap_id, pp_max FROM scraped_beatmaps ORDER BY RANDOM() LIMIT 100")
        
        valid_maps = []
        tasks = []

        # Исправленная структура цикла выбора карт
        for r in scraped:
            data = await bot.osu_client.request(f"beatmaps/{r['beatmap_id']}")
            
            # Все проверки теперь внутри цикла (с правильным отступом)
            if not data or data.get('mode_int') != 3:
                continue

            sr = data['difficulty_rating']
            if not (median_sr - 0.5 <= sr <= median_sr + 0.8):
                continue

            # Создаём задачу на получение PP
            task = asyncio.create_task(_get_map_top_pp(bot, data['id']))
            tasks.append((data, task))

            if len(tasks) >= 4:
                break

        # Ожидаем завершения всех сетевых запросов PP
        for data, task in tasks:
            top_pp = await task
            # Балансировка (0.95 — множитель для реалистичности)
            realistic_pp = top_pp * 0.95 if top_pp > 0 else 0

            valid_maps.append({
                "bm": data,
                "pp": realistic_pp
            })
        
        if not valid_maps:
            return await interaction.followup.send("Ничего не нашлось под ваш SR в базе данных.")

        # Формирование текстового сообщения
        desc = [f"**Медианный SR: {median_sr:.2f}★**\n"]
        for i, m in enumerate(valid_maps):
            bm = m['bm']
            pattern = _infer_pattern_label(bm)
            stars = bm['difficulty_rating']
            bpm = int(bm.get("bpm") or 0)
            objects = int(bm.get("count_circles") or 0) + int(bm.get("count_sliders") or 0)
            
            line = (
                f"**[{i+1}] [{bm['beatmapset']['title']}](https://osu.ppy.sh/b/{bm['id']}) [[{bm['version']}]]**\n"
                f"SR **{stars:.2f}★** • (~{m['pp']:.1f}PP) • BPM {bpm}\n"
                f"Objects {objects} • Pattern *{pattern}*\n"
            )
            desc.append(line)
            
        embed = discord.Embed(title=f"Рекомендации для {row['osu_username']}", description="\n".join(desc), color=0x2f3136)
        
        first = valid_maps[0]
        pp_vals = {"95": first['pp']*0.85, "98": first['pp']*0.93, "99": first['pp']*0.97, "100": first['pp']}
        
        img_buf = await generate_map_card(bot, first['bm'], pp_vals)
        file = discord.File(img_buf, filename="card.png")
        embed.set_image(url="attachment://card.png")

        await interaction.followup.send(file=file, embed=embed)

    @bot.tree.command(name="profile", description="Посмотреть профиль игрока")
    async def profile(interaction: discord.Interaction, username: str):
        await interaction.response.send_message(f"Запрос профиля {username}... (в разработке)")