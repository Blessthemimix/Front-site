from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict

import asyncio
import logging
import random
import time
from io import BytesIO, StringIO
from typing import Any
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageFont, ImageFilter
import io
import aiohttp

import discord
from colorthief import ColorThief
from discord.ext import commands, tasks
from discord import app_commands

from .config import Settings
from .db import get_db_conn
from .osu_client import OsuClient
from .verification import VerificationInput, compute_digit_value

logger = logging.getLogger(__name__)

def _parse_username_list(raw: str) -> list[str]:
    raw = raw.strip()
    if not raw:
        return []
    if "," in raw or ";" in raw or "\n" in raw:
        parts: list[str] = []
        for chunk in raw.replace(";", ",").replace("\n", ",").split(","):
            c = chunk.strip()
            if c:
                parts.append(c)
        return parts
    return [p for p in raw.split() if p]

def get_all_digit_role_ids(role_mapping: dict[str, dict[int, int]]) -> set[int]:
    return {role_id for mode_map in role_mapping.values() for role_id in mode_map.values()}

async def _ambient_color_from_avatar(bot: RoleBot, avatar_url: str | None) -> discord.Color:
    if not avatar_url:
        return discord.Color.blurple()
    try:
        response = await bot.osu_client._http.get(avatar_url)
        response.raise_for_status()
        color = ColorThief(BytesIO(response.content)).get_color(quality=1)
        return discord.Color.from_rgb(*color)
    except Exception:
        return discord.Color.blurple()

async def _ambient_color_from_image(bot: RoleBot, image_url: str | None) -> discord.Color:
    if not image_url:
        return discord.Color.blurple()
    try:
        response = await bot.osu_client._http.get(image_url)
        response.raise_for_status()
        color = ColorThief(BytesIO(response.content)).get_color(quality=1)
        return discord.Color.from_rgb(*color)
    except Exception:
        return discord.Color.blurple()

def _short_num(value: Any, decimals: int = 2) -> str:
    try:
        n = float(value)
    except Exception:
        return "?"
    abs_n = abs(n)
    if abs_n >= 1_000_000:
        return f"{n/1_000_000:.{decimals}f}M"
    if abs_n >= 1_000:
        return f"{n/1_000:.{decimals}f}K"
    return f"{n:.{decimals}f}"

def _score_accuracy_percent(score: dict[str, Any]) -> float | None:
    raw = score.get("accuracy")
    if raw is None: return None
    try:
        a = float(raw)
    except (TypeError, ValueError): return None
    if a <= 1.0: return a * 100.0
    return a

def _ruleset_id_from_beatmap(bm: dict[str, Any]) -> int:
    mi = bm.get("mode_int")
    if mi is not None: return int(mi)
    mode = str(bm.get("mode") or "osu")
    return {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}.get(mode, 0)

def _mods_for_attributes(score: dict[str, Any]) -> list[dict[str, Any]]:
    mods_raw = score.get("mods")
    if not mods_raw or not isinstance(mods_raw, list): return []
    out: list[dict[str, Any]] = []
    for m in mods_raw:
        if isinstance(m, dict) and m.get("acronym"):
            item: dict[str, Any] = {"acronym": m["acronym"]}
            if m.get("settings"): item["settings"] = m["settings"]
            out.append(item)
    return out

async def _star_rating_for_score(bot: RoleBot, score: dict[str, Any]) -> float:
    bm = score.get("beatmap") or {}
    mods = _mods_for_attributes(score)

    # Если модов нет, берем SR напрямую из данных карты, не мучая API
    has_sr_mods = any(m.get('acronym') in ('DT', 'NC', 'HT', 'DC', 'HR', 'EZ') for m in mods)
    if not has_sr_mods:
        return float(bm.get("difficulty_rating") or 0)

    bid = bm.get("id")
    if bid is None: return float(bm.get("difficulty_rating") or 0)

    rid = _ruleset_id_from_beatmap(bm)
    # Запрашиваем только если моды реально меняют звезды
    got = await bot.osu_client.beatmap_star_rating(int(bid), mods, rid)
    return got if got is not None else float(bm.get("difficulty_rating") or 0)

def _infer_pattern_label(bm: dict[str, Any], *, effective_sr: float | None = None) -> str:
    circles = int(bm.get("count_circles") or 0)
    sliders = int(bm.get("count_sliders") or 0)
    spinners = int(bm.get("count_spinners") or 0)
    objects = circles  sliders  spinners
    total = max(objects, 1)
    slider_ratio = sliders / total
    bpm = float(bm.get("bpm") or 0)
    length_sec = max(float(bm.get("hit_length") or bm.get("total_length") or 1), 1.0)
    nps = objects / length_sec
    sr = float(effective_sr) if effective_sr is not None else float(bm.get("difficulty_rating") or 0.0)

    if slider_ratio > 0.61: ln_label = "Full LN"
    elif slider_ratio >= 0.35: ln_label = "Heavy LN"
    elif slider_ratio >= 0.18: ln_label = "Hybrid"
    else: ln_label = "Rice"

    tags: list[str] = []
    if length_sec >= 300: tags.append("Stamina")

    primary: str | None = None
    riceish = slider_ratio < 0.32
    streamish = 0.22 <= slider_ratio <= 0.52

    if bpm >= 270: primary = "Speed"
    elif 240 <= bpm < 270 and nps >= 11.0 and slider_ratio <= 0.48: primary = "Speedjack"
    elif 155 <= bpm <= 205:
        if nps >= 10.5 and riceish: primary = "Dance jack"
        elif nps >= 7.0: primary = "Dance stream"
        else: primary = "Dance"
    elif 175 <= bpm < 265 and nps >= 10.0 and riceish: primary = "Chordjack"
    elif 185 <= bpm < 240 and streamish and nps >= 8.0: primary = "Handstream"
    elif 170 <= bpm < 235 and 8.0 <= nps < 12.5 and riceish: primary = "Jumpstream"
    elif bpm >= 220 and nps >= 9.0: primary = "Stream"
    elif bpm >= 195: primary = "Mid-high"
    elif bpm >= 170: primary = "Finger control"
    else: primary = "Control"

    if primary: tags.append(primary)
    if sr >= 6.2 and nps < 6.5: tags.append("Reading")
    elif sr >= 5.8 and nps < 7.5 and slider_ratio < 0.4: tags.append("Tech")

    out: list[str] = [ln_label]
    seen: set[str] = {ln_label}
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return " / ".join(out)

class RoleBot(commands.Bot):
    def __init__(self, *, settings: Settings, osu_client: OsuClient, role_mapping: dict[str, dict[int, int]]) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.osu_client = osu_client
        self.role_mapping = role_mapping

    async def setup_hook(self) -> None:
        # ПРОВЕРКА И СОЗДАНИЕ ТАБЛИЦЫ ПРИ ЗАПУСКЕ
        async with get_db_conn() as db_conn:
            await db_conn.execute("""
                CREATE TABLE IF NOT EXISTS scraped_beatmaps (
                    beatmap_id BIGINT PRIMARY KEY,
                    title TEXT,
                    pp_max FLOAT,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        self.poll_pending_assignments.start() # This line now correctly refers to the decorated method
        await self.tree.sync()
        logger.info("Database tables verified and slash commands synced")

    async def close(self) -> None:
        self.poll_pending_assignments.cancel()
        await self.osu_client.close()
        await super().close()

    # Обертка для рисования скругленного прямоугольника (в старых Pillow этого нет)
def _draw_rounded_rect(draw, coords, radius, color):
    # Костыль для скругления, если Pillow старая
    # draw.rounded_rectangle(coords, radius, fill=color) # На новых Pillow работает

    # Версия для Pillow < 9
    x, y, w, h = coords[0], coords[1], coords[2] - coords[0], coords[3] - coords[1]

    draw.ellipse((x, y, x  radius, y  radius), fill=color)
    draw.ellipse((x  w - radius, y, x  w, y  radius), fill=color)
    draw.ellipse((x, y  h - radius, x  radius, y  h), fill=color)
    draw.ellipse((x  w - radius, y  h - radius, x  w, y  h), fill=color)

    draw.rectangle((x  radius/2, y, x  w - radius/2, y  h), fill=color)
    draw.rectangle((x, y  radius/2, x  w, y  h - radius/2), fill=color)

async def generate_map_card(bot: RoleBot, bm_data: dict, pp_values: dict):
    """
    Генерирует карточку в стиле osu! баннера.
    pp_values должен быть словарем: {"95": 100, "98": 120, "99": 130, "100": 150}
    """
    W, H = 1200, 600
    card = Image.new('RGBA', (W, H), (15, 27, 28, 255))
    draw = ImageDraw.Draw(card)

    # 1. Загрузка фона (обложка карты)
    cover_url = bm_data["beatmapset"]["covers"]["cover@2x"]
    async with aiohttp.ClientSession() as session:
        async def fetch_img(url):
            async with session.get(url) as resp:
                return await resp.read() if resp.status == 200 else None

        cover_data = await fetch_img(cover_url)
        if cover_data:
            bg = Image.open(BytesIO(cover_data)).convert("RGBA")
            # Ресайз с сохранением пропорций и обрезка
            bg_w, bg_h = bg.size
            ratio = W / bg_w
            bg = bg.resize((W, int(bg_h * ratio)), Image.Resampling.LANCZOS)
            card.paste(bg, (0, 0))

    # 2. Затемнение фона (градиент снизу)
    overlay = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for y in range(H):
        alpha = int(140  (y / H) * 115) # Постепенное затемнение до 255
        odraw.line([(0, y), (W, y)], fill=(15, 27, 28, alpha))
    card = Image.alpha_composite(card, overlay)
    draw = ImageDraw.Draw(card) # Пересоздаем draw для рисования поверх композита

    # 3. Шрифты
    try:
        f_title = ImageFont.truetype("Roboto-Bold.ttf", 45)
        f_sub = ImageFont.truetype("Roboto-Regular.ttf", 28)
        f_stat = ImageFont.truetype("Roboto-Bold.ttf", 24)
        f_pp_lab = ImageFont.truetype("Roboto-Regular.ttf", 32)
        f_pp_val = ImageFont.truetype("Roboto-Bold.ttf", 32)
    except IOError:
        logger.warning("Could not load Roboto fonts, falling back to default.")
        # Fallback to default if fonts are not found
        f_title = ImageFont.load_default()
        f_sub = ImageFont.load_default()
        f_stat = ImageFont.load_default()
        f_pp_lab = ImageFont.load_default()
        f_pp_val = ImageFont.load_default()

    # 4. Текст: Название и Автор
    title_text = bm_data["beatmapset"]["title"]
    artist_diff = f"{bm_data['beatmapset']['artist']} // [{bm_data['version']}]"
    draw.text((50, 320), title_text[:50], font=f_title, fill=(255, 230, 150))
    draw.text((50, 380), artist_diff[:70], font=f_sub, fill=(200, 200, 200))

    # 5. Левая колонка: Stars и Bars (CS, AR, HP, OD)
    sr = bm_data["difficulty_rating"]
    draw.text((50, 440), "★" * int(sr)  f" {sr:.2f}", font=f_pp_val, fill=(255, 198, 0))

    stats = [
        ("CS", bm_data["cs"]),
        ("AR", bm_data["ar"]),
        ("HP", bm_data["drain"]),
        ("OD", bm_data["accuracy"])
    ]

    start_y = 490
    for label, val in stats:
        draw.text((50, start_y), label, font=f_stat, fill=(200, 200, 200))
        # Рисуем полоску
        bar_w = 200
        try:
            fill_w = int((float(val) / 10) * bar_w)
        except (ValueError, TypeError):
            fill_w = 0
        draw.rounded_rectangle([100, start_y  8, 100  bar_w, start_y  18], radius=5, fill=(50, 50, 50))
        draw.rounded_rectangle([100, start_y  8, 100  fill_w, start_y  18], radius=5, fill=(255, 198, 0))
        draw.text((310, start_y), f"{val:.1f}", font=f_stat, fill=(255, 255, 255))
        start_y = 35

    # 6. Центр: Создатель карты (Аватарка и ник)
    creator_id = bm_data["beatmapset"]["user_id"]
    creator_name = bm_data["beatmapset"]["creator"]
    avatar_data = await fetch_img(f"https://a.ppy.sh/{creator_id}")
    if avatar_data:
        av = Image.open(BytesIO(avatar_data)).convert("RGBA").resize((120, 120), Image.Resampling.LANCZOS)
        # Маска для скругления
        mask = Image.new('L', (120, 120), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, 120, 120], radius=20, fill=255)
        card.paste(av, (540, 420), mask)

    draw.text((600, 555), creator_name, font=f_sub, fill=(255, 255, 255), anchor="mt")

    # 7. Правая колонка: Метаданные и PP
    # Время, Комбо, БПМ
    length = f"{int(bm_data['total_length']//60)}:{int(bm_data['total_length']%60):02d}"
    meta_text = f"🕒 {length}  🔗 {bm_data.get('max_combo', 0)}x  🥁 {bm_data['bpm']} bpm"
    draw.text((1150, 380), meta_text, font=f_sub, fill=(200, 200, 200), anchor="ra")

    pp_y = 430
    for acc in ["95", "98", "99", "100"]:
        val = pp_values.get(acc, 0)
        draw.text((850, pp_y), f"{acc}%:", font=f_pp_lab, fill=(180, 180, 180))
        draw.text((1150, pp_y), f"{val:.2f}pp", font=f_pp_val, fill=(255, 255, 255), anchor="ra")
        pp_y = 45

    # 8. Финальное скругление всей карточки
    final_mask = Image.new('L', (W, H), 0)
    ImageDraw.Draw(final_mask).rounded_rectangle([0, 0, W, H], radius=40, fill=255)

    output = Image.new('RGBA', (W, H), (0,0,0,0))
    output.paste(card, (0, 0), final_mask)

    buf = BytesIO()
    output.save(buf, format='PNG') # Changed to PNG for better quality background
    buf.seek(0)
    return buf

     # --------------------------------------------------------------------- #
   #  Background task: poll the DB for pending role‑assignments            #
   # --------------------------------------------------------------------- #
      @tasks.loop(seconds=10.0)  # you can adjust the interval if you wish
        async def poll_pending_assignments(self) -> None:
        """Poll the database for pending role‑assignments and process them."""
        guild = self.get_guild(self.settings.discord_guild_id)
        if guild is None:
            logger.warning(
                f"Guild with ID {self.settings.discord_guild_id} not found – "
                "skipping this poll cycle."
            )
            return

        async with get_db_conn() as db_conn:
            try:
                rows = await db_conn.fetch(
                    "SELECT id, discord_id, role_id FROM pending_role_assignments "
                    "WHERE status='pending' LIMIT 20"
                )
                if not rows:
                    return  # nothing to do this round

                for row in rows:
                    try:
                        # ---- fetch member -------------------------------------------------
                        member = guild.get_member(row["discord_id"])
                        if member is None:
                            try:
                                member = await guild.fetch_member(row["discord_id"])
                            except discord.NotFound:
                                logger.warning(
                                    f"Member {row['discord_id']} missing – marking assignment failed."
                                )
                                await db_conn.execute(
                                    "UPDATE pending_role_assignments SET status='failed', "
                                    "processed_at=$1, error_message=$2 WHERE id=$3",
                                    int(time.time()),
                                    "Member not found",
                                    row["id"],
                                )
                                continue
                            except discord.Forbidden:
                                logger.warning(
                                    f"Permission error while fetching member {row['discord_id']} – "
                                    "marking assignment failed."
                                )
                                await db_conn.execute(
                                    "UPDATE pending_role_assignments SET status='failed', "
                                    "processed_at=$1, error_message=$2 WHERE id=$3",
                                    int(time.time()),
                                    "Permission error fetching member",
                                    row["id"],
                                )
                                continue

                        # ---- fetch role ---------------------------------------------------
                        role = guild.get_role(row["role_id"])
                        if role is None:
                            logger.warning(
                                f"Role {row['role_id']} missing – marking assignment failed."
                            )
                            await db_conn.execute(
                                "UPDATE pending_role_assignments SET status='failed', "
                                "processed_at=$1, error_message=$2 WHERE id=$3",
                                int(time.time()),
                                "Role not found",
                                row["id"],
                            )
                            continue

                        # ---- do the replacement -------------------------------------------
                        await self._replace_digit_roles(member, role)
                        await db_conn.execute(
                            "UPDATE pending_role_assignments SET status='done', processed_at=$1 WHERE id=$2",
                            int(time.time()),
                            row["id"],
                        )
                        logger.info(
                            f"Assigned role {role.name} to {member.display_name} (ID {member.id})"
                        )

                    except discord.Forbidden:
                        logger.error(
                            "Missing Manage‑Roles permission or role hierarchy problem. "
                            f"Failed to assign role {row['role_id']} to member {row['discord_id']}."
                        )
                        await db_conn.execute(
                            "UPDATE pending_role_assignments SET status='failed', "
                            "processed_at=$1, error_message=$2 WHERE id=$3",
                            int(time.time()),
                            "Discord permission error",
                            row["id"],
                        )
                    except Exception as exc:  # pragma: no cover – defensive
                        logger.exception(
                            f"Unexpected error while processing assignment {row['id']}"
                        )
                        await db_conn.execute(
                            "UPDATE pending_role_assignments SET status='failed', "
                            "processed_at=$1, error_message=$2 WHERE id=$3",
                            int(time.time()),
                            str(exc),
                            row["id"],
                        )
            except Exception as db_exc:  # pragma: no cover – defensive
                logger.exception("Database error during role‑assignment poll")


def register_commands(bot: RoleBot) -> None:

    @bot.tree.command(name="linkcode", description="Связать Discord с профилем через код с сайта")
    async def linkcode(interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer(ephemeral=True)
        code = code.strip().upper()
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_username, discord_id FROM verification_challenges WHERE link_code = $1 AND status = 'pending'", code)
            if row is None:
                await interaction.followup.send(f"❌ Код `{code}` не найден.")
                return
            if int(row["discord_id"]) != int(interaction.user.id):
                await interaction.followup.send("❌ Этот код выдан другому Discord ID.")
                return
            
            # Check if the user already has a verified link
            existing_link = await db_conn.fetchrow("SELECT verified_at FROM verified_discord_links WHERE discord_id = $1", int(interaction.user.id))
            if existing_link:
                await interaction.followup.send(f"⚠️ Ваш аккаунт уже связан с osu! профилем **{row['osu_username']}**. Если вы хотите изменить его, обратитесь к администратору.")
                return

            await db_conn.execute("INSERT INTO verified_discord_links (discord_id, osu_id, osu_username, verified_at) VALUES ($1, $2, $3, $4)", int(interaction.user.id), row["osu_id"], row["osu_username"], int(time.time()))
            await db_conn.execute("UPDATE verification_challenges SET status = 'completed' WHERE link_code = $1", code) # Mark challenge as completed
        
        await interaction.followup.send(f"✅ Аккаунт **{row['osu_username']}** привязан!")

    @bot.command(name="sync")
    @commands.is_owner()
    async def sync(ctx: commands.Context):
        await bot.tree.sync()
        await ctx.send("✅ Команды синхронизированы!")

    @bot.tree.command(name="profile", description="Показать статистику игрока")
    async def profile(interaction: discord.Interaction, username: str | None = None):
        await interaction.response.defer()
        target = username
        if not target:
            async with get_db_conn() as db_conn:
                row = await db_conn.fetchrow("SELECT osu_username FROM verified_discord_links WHERE discord_id=$1", int(interaction.user.id))
                if row: target = row["osu_username"]
                else:
                    await interaction.followup.send("Укажите ник или привяжите профиль командой `/linkcode`.")
                    return

        try:
            user_data = await bot.osu_client.request(f"users/{quote(target.strip(), safe='')}")
            if not user_data:
                await interaction.followup.send(f"❌ Игрок **{target}** не найден.")
                return
        except aiohttp.ClientResponseError as e:
            await interaction.followup.send(f"❌ Произошла ошибка при поиске игрока: {e.status} - {e.message}")
            return
        except Exception as e:
            logger.error(f"Error fetching osu user {target}: {e}", exc_info=True)
            await interaction.followup.send("❌ Произошла неизвестная ошибка при поиске игрока.")
            return

        stats = user_data.get("statistics") or {}
        ambient_color = await _ambient_color_from_avatar(bot, user_data.get("avatar_url"))
        embed = discord.Embed(title=f"{user_data.get('country', {}).get('code', '')} {user_data['username']}", color=ambient_color)
        if user_data.get("avatar_url"): embed.set_thumbnail(url=user_data["avatar_url"])

        embed.description = f"**{stats.get('pp', 0):,.2f}pp** • #{stats.get('global_rank', 0):,}"
        embed.add_field(name="Accuracy", value=f"{stats.get('hit_accuracy', 0):.2f}%", inline=True)
        embed.add_field(name="Play time", value=f"{int((stats.get('play_time') or 0) / 3600):,} hrs", inline=True)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="recommend", description="Адекватные рекомендации с карточкой")
    async def recommend(interaction: discord.Interaction):
        await interaction.response.defer()

        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id, osu_username FROM users WHERE discord_id=$1"
                                         " UNION SELECT osu_id, osu_username FROM verified_discord_links WHERE discord_id=$1",
                                         int(interaction.user.id))

        if not row:
            await interaction.followup.send("Сначала привяжите профиль командой `/linkcode`.")
            return

        osu_id, username = row["osu_id"], row["osu_username"]

        # 1. Загружаем данные
        try:
            best, recent = await asyncio.gather(
                bot.osu_client.request(f"users/{osu_id}/scores/best?limit=100"),
                bot.osu_client.request(f"users/{osu_id}/scores/recent?limit=50&include_fails=0")
            )
        except Exception as e:
            logger.error(f"Error fetching scores for user {username} (ID: {osu_id}): {e}", exc_info=True)
            await interaction.followup.send("Произошла ошибка при получении ваших игровых данных.")
            return

        best = best or []
        recent = recent or []

        # 2. Анализ скилла
        pattern_stats = defaultdict(list)
        all_relevant_srs = []
        combined_scores = recent  recent  best

        # Fetch beatmap data for more accurate SR calculation if needed
        tasks_sr = []
        scores_to_process = []
        for s in combined_scores:
            bm = s.get('beatmap')
            ruleset_id = _ruleset_id_from_beatmap(bm) if bm else -1
            if bm and ruleset_id == 3: # Mania only
                mods = _mods_for_attributes(s)
                has_sr_mods = any(m.get('acronym') in ('DT', 'NC', 'HT', 'DC', 'HR', 'EZ') for m in mods)
                if has_sr_mods:
                    tasks_sr.append(bot.osu_client.beatmap_star_rating(int(bm['id']), mods, ruleset_id))
                else:
                    # If no SR-modifying mods, use difficulty_rating directly
                    score_sr = float(bm.get("difficulty_rating") or 0)
                    pattern = _infer_pattern_label(bm, effective_sr=score_sr)
                    pattern_stats[pattern].append(score_sr)
                    all_relevant_srs.append(score_sr)
                    scores_to_process.append({"score": s, "sr": score_sr, "pattern": pattern})
            elif bm and ruleset_id != 3:
                 logger.debug(f"Skipping non-mania beatmap ID {bm.get('id')} for user {username}")
            else:
                 logger.warning(f"Score missing beatmap data for user {username}: {s.get('beatmapset', {}).get('title')}")
        
        if tasks_sr:
            generated_srs = await asyncio.gather(*tasks_sr)
            sr_task_idx = 0
            for s in combined_scores:
                bm = s.get('beatmap')
                if bm and _ruleset_id_from_beatmap(bm) == 3:
                    mods = _mods_for_attributes(s)
                    has_sr_mods = any(m.get('acronym') in ('DT', 'NC', 'HT', 'DC', 'HR', 'EZ') for m in mods)
                    if has_sr_mods:
                        score_sr = generated_srs[sr_task_idx] if sr_task_idx < len(generated_srs) else float(bm.get("difficulty_rating") or 0)
                        sr_task_idx = 1
                        pattern = _infer_pattern_label(bm, effective_sr=score_sr)
                        pattern_stats[pattern].append(score_sr)
                        all_relevant_srs.append(score_sr)
                        scores_to_process.append({"score": s, "sr": score_sr, "pattern": pattern})
        
        # Filter out scores with invalid SR or pattern before calculating median
        valid_srs = [sr for sr in all_relevant_srs if sr > 0]
        if not valid_srs:
            await interaction.followup.send("Недостаточно данных для анализа. Сыграй пару карт, чтобы получить рекомендации.")
            return

        valid_srs.sort()
        base_comfort_sr = valid_srs[len(valid_srs) // 2]

        # 3. Подбор кандидатов
        played_ids = {int(s['beatmap']['id']) for s in combined_scores if s.get('beatmap')}
        
        try:
            async with get_db_conn() as db_conn:
                # Fetch more maps to increase chances of finding good recommendations
                scraped_rows = await db_conn.fetch("SELECT beatmap_id, title, pp_max FROM scraped_beatmaps WHERE beatmap_id NOT IN ($1) ORDER BY RANDOM() LIMIT 250", tuple(played_ids))
        except Exception as db_err:
            logger.error(f"Database error fetching scraped beatmaps: {db_err}", exc_info=True)
            await interaction.followup.send("Произошла ошибка при доступе к базе карт.")
            return

        if not scraped_rows:
            await interaction.followup.send("База карт пуста. Пожалуйста, добавьте карты командой `/scrape_top`.")
            return

        potential_bids = [r['beatmap_id'] for r in scraped_rows]
        
        # Fetch beatmap data in batches to avoid exceeding Discord rate limits or osu! API limits
        maps_data_futures = []
        for bid_chunk in [potential_bids[i:i  50] for i in range(0, len(potential_bids), 50)]:
            if bid_chunk:
                tasks_chunk = [bot.osu_client.request(f"beatmaps?ids={','.join(map(str, bid_chunk))}")]
                maps_data_futures.extend(tasks_chunk)
        
        fetched_beatmaps_list = await asyncio.gather(*maps_data_futures)
        
        all_fetched_maps = []
        for chunk_result in fetched_beatmaps_list:
            if chunk_result and 'beatmaps' in chunk_result:
                all_fetched_maps.extend(chunk_result['beatmaps'])
        
        # Create a mapping from beatmap ID to its data for easy lookup
        beatmap_data_map = {bm['id']: bm for bm in all_fetched_maps}
        scraped_map_data_map = {r['beatmap_id']: r for r in scraped_rows}

        final_picks = []
        current_margin_step = 0.15 # Start with a smaller margin and increase
        max_margin = 0.8 # Maximum star rating difference to consider 

        # Prioritize maps closer to the median SR
        while len(final_picks) < 4 and current_margin_step <= max_margin:
            temp_picks = []
            for bid, map_info in scraped_map_data_map.items():
                if bid not in beatmap_data_map:
                    continue
                bm_data = beatmap_data_map[bid]
                
                if _ruleset_id_from_beatmap(bm_data) != 3: continue # Mania only
                
                map_sr = float(bm_data.get("difficulty_rating") or 0.0)
                pattern = _infer_pattern_label(bm_data)
                is_ln = "LN" in pattern
                
                lower_bound = base_comfort_sr - current_margin_step
                upper_bound = base_comfort_sr  (current_margin_step * 1.5 if is_ln else current_margin_step) # Slightly wider range for LN maps

                if lower_bound <= map_sr <= upper_bound:
                    pp_val = map_info['pp_max'] # Use pp_max from scraped data for consistency
                    # Calculate a score based on SR difference and pattern popularity
                    sr_diff = abs(base_comfort_sr - map_sr)
                    pattern_weight = len(pattern_stats.get(pattern, [])) / (len(combined_scores)  1e-6) # Avoid division by zero
                    
                    # Score: Lower is better (smaller SR diff, less common pattern relative to user's plays)
                    score = (sr_diff * 2) - (pattern_weight * 0.5) 
                    
                    temp_picks.append({"bm": bm_data, "sr": map_sr, "pp": pp_val, "pattern": pattern, "score": score})
            
            if temp_picks:
                temp_picks.sort(key=lambda x: x["score"])
                # Add unique picks until we have 4
                for pick in temp_picks:
                    if len(final_picks) < 4 and pick["bm"]["id"] not in {p["bm"]["id"] for p in final_picks}:
                        final_picks.append(pick)
            
            current_margin_step = 0.15 # Increase margin for the next iteration

        if not final_picks:
            await interaction.followup.send("Не удалось подобрать карты с учетом ваших предпочтений. Попробуйте сыграть больше карт или свяжитесь с разработчиком.")
            return

        # --- ВОТ ЗДЕСЬ НАЧИНАЮТСЯ ИЗМЕНЕНИЯ ДЛЯ ВЫВОДА ---

        # Берем первую карту из списка для генерации основной картинки
        main_pick = final_picks[0]
        
        # Подготавливаем словарь PP для отрисовки
        # Use a function to calculate approximate PP values for different acc levels
        async def calculate_pp(beatmap_id, pp_max, accuracy):
            # This is a placeholder. Real PP calculation requires complex logic or an external API.
            # For demonstration, we'll scale the max PP.
            # You might want to integrate with a PP calculator library or API here.
            return pp_max * (accuracy / 100.0) ** 2 # A very rough approximation

        pp_map = {}
        for acc_str in ["95", "98", "99", "100"]:
            acc_float = float(acc_str)
            # Simulate PP calculation - replace with actual logic if possible
            pp_map[acc_str] = await calculate_pp(main_pick["bm"]["id"], main_pick["pp"], acc_float)

        # Генерируем изображение (функция, которую я давал ранее)
        try:
            img_buf = await generate_map_card(bot, main_pick["bm"], pp_map)
            file = discord.File(fp=img_buf, filename="recommendation.png")
        except Exception as e:
            logger.error(f"Error generating map card: {e}", exc_info=True)
            await interaction.followup.send("Произошла ошибка при генерации карточки рекомендации.")
            return

        # Формируем текстовый список всех 4 карт для Embed
        blocks = []
        for idx, p in enumerate(final_picks, 1):
            bm = p["bm"]
            url = f"https://osu.ppy.sh/b/{bm['id']}"
            star = "⭐" if idx == 1 else "🔹"
            blocks.append(
                f"{star} **[{bm['beatmapset']['title']} [{bm['version']}]]({url})**\n"
                f"└ {p['sr']:.2f}★ | {p['pattern']} | ~{p['pp']:.0f}pp"
            )

        # Создаем Embed
        embed = discord.Embed(
            title=f"Рекомендации для {interaction.user.display_name}",
            description=f"Твой медианный SR: **{base_comfort_sr:.2f}★**\n\n"  "\n".join(blocks),
            color=await _ambient_color_from_image(bot, main_pick["bm"]["beatmapset"]["covers"]["cover@2x"])
        )

        # Привязываем сгенерированную картинку к Embed
        embed.set_image(url="attachment://recommendation.png")
        embed.set_footer(text=f"Карточка: {main_pick['bm']['beatmapset']['title']} [{main_pick['bm']['version']}]")

        # Отправляем всё вместе (Embed  Файл)
        await interaction.followup.send(embed=embed, file=file)

    @bot.tree.command(name="scrape_top", description="Собрать карты из топ-скоров нескольких игроков")
    @app_commands.describe(usernames="Ники через запятую или пробел", per_user="Сколько карт с каждого (1–100)")
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id) # Cooldown per user
    async def scrape_top(interaction: discord.Interaction, usernames: str, per_user: int = 50) -> None:
        await interaction.response.defer()
        names = _parse_username_list(usernames)
        if not names:
            await interaction.followup.send("Укажите хотя бы один ник.")
            return
        
        if not (1 <= per_user <= 100):
            await interaction.followup.send("Количество карт с пользователя должно быть от 1 до 100.")
            return

        async def fetch_user_top(name):
            quoted_name = quote(name.strip(), safe='')
            try:
                u_data = await bot.osu_client.request(f"users/{quoted_name}")
                if not u_data:
                    logger.warning(f"User '{name}' not found on osu!")
                    return (name, None, None) # Return original name for reporting
                
                user_id = u_data['id']
                user_name_actual = u_data['username'] # Get the actual username from API
                
                scores = await bot.osu_client.request(f"users/{user_id}/scores/best?limit={per_user}")
                
                return (user_name_actual, scores or [], user_id)
            except aiohttp.ClientResponseError as e:
                logger.error(f"osu! API error fetching top scores for {name}: {e.status} - {e.message}")
                return (name, None, None) # Error occurred
            except Exception as e:
                logger.error(f"Unexpected error fetching top scores for {name}: {e}", exc_info=True)
                return (name, None, None)

        # Fetch users concurrently
        results = await asyncio.gather(*(fetch_user_top(n) for n in names))
        
        unique_maps_to_db = {}
        processed_user_info = [] # Store (original_name, actual_username, list_of_scores)
        
        for original_name, scores, user_id in results:
            if scores is None:
                processed_user_info.append((original_name, None, [])) # Indicate user not found or error
            else:
                # Find the actual username if it's different from the input
                actual_username = original_name
                if user_id:
                    # Find the entry in results that matches this user_id to get the actual username
                    for res_uname, res_scores, res_id in results:
                        if res_id == user_id:
                            actual_username = res_uname
                            break

                processed_user_info.append((original_name, actual_username, scores))
                
                for s in scores:
                    # Ensure beatmap data is present and it's a mania map
                    bm = s.get('beatmap')
                    if not bm or _ruleset_id_from_beatmap(bm) != 3: continue

                    bid = bm['id']
                    pp = float(s.get('pp') or 0)
                    
                    # Store map data, prioritizing higher PP scores for the same map
                    if bid not in unique_maps_to_db or pp > unique_maps_to_db[bid]['pp_max']:
                        unique_maps_to_db[bid] = {
                            'title': f"{bm['beatmapset']['title']} [{bm['version']}]",
                            'pp_max': pp,
                            'user': actual_username # Store the actual username
                        }

        if not unique_maps_to_db:
            error_messages = []
            for orig_name, actual_name, scores in processed_user_info:
                if scores is None:
                    error_messages.append(f"- **{orig_name}**: Не найден или произошла ошибка.")
                elif not scores and actual_name:
                    error_messages.append(f"- **{actual_name}**: Не найдено карт в топ {per_user}.")
            
            if error_messages:
                await interaction.followup.send("Не удалось собрать данные:\n"  "\n".join(error_messages))
            else:
                await interaction.followup.send("Не удалось собрать данные. Проверьте ники и попробуйте снова.")
            return

        # Mass insert/update into the database
        db_entries = [(bid, data['title'], data['pp_max']) for bid, data in unique_maps_to_db.items()]
        try:
            async with get_db_conn() as db_conn:
                await db_conn.executemany("""
                    INSERT INTO scraped_beatmaps (beatmap_id, title, pp_max)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (beatmap_id) DO UPDATE SET pp_max = EXCLUDED.pp_max, title = EXCLUDED.title
                """, db_entries)
        except Exception as db_exc:
            logger.error(f"Database error during scrape_top insertion: {db_exc}", exc_info=True)
            await interaction.followup.send("Произошла ошибка при сохранении карт в базу данных.")
            return

        # Prepare log file content
        log_content_lines = []
        # Sort by PP descending for the log file
        sorted_maps = sorted(unique_maps_to_db.items(), key=lambda item: -item[1]["pp_max"])
        
        for bid, data in sorted_maps:
            log_content_lines.append(f"{data['title']}")
            log_content_lines.append(f"https://osu.ppy.sh/b/{bid}")
            log_content_lines.append(f"{data['pp_max']:.0f}pp • Collected from: {data['user']}")
            log_content_lines.append("") # Empty line for separation

        log_file_content = "\n".join(log_content_lines)
        
        file = discord.File(
            fp=BytesIO(log_content.encode("utf-8")),
            filename="scrape_top_maps.txt"
        )

        # Construct success message
        successful_users_str = ", ".join(
            f"**{actual_name}** ({len(scores)} maps)"
            for _, actual_name, scores in processed_user_info
            if actual_name is not None and scores is not None and len(scores) > 0
        )
        
        if not successful_users_str:
             await interaction.followup.send("Не удалось собрать данные ни с одного из указанных пользователей.")
             return
        
        await interaction.followup.send(
            f"Успешно собрано **{len(unique_maps_to_db)}** уникальных карт для команды.\n"
            f"Собрано с пользователей: {successful_users_str}",
            file=file
        )