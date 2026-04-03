"""Discord bot implementation and role assignment worker."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from io import BytesIO, StringIO
from typing import Any
from urllib.parse import quote

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
    bid = bm.get("id")
    if bid is None: return float(bm.get("difficulty_rating") or 0)
    rid = _ruleset_id_from_beatmap(bm)
    mods = _mods_for_attributes(score)
    got = await bot.osu_client.beatmap_star_rating(int(bid), mods, rid)
    return got if got is not None else float(bm.get("difficulty_rating") or 0)

def _infer_pattern_label(bm: dict[str, Any], *, effective_sr: float | None = None) -> str:
    circles = int(bm.get("count_circles") or 0)
    sliders = int(bm.get("count_sliders") or 0)
    spinners = int(bm.get("count_spinners") or 0)
    objects = circles + sliders + spinners
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
        
        self.poll_pending_assignments.start()
        await self.tree.sync()
        logger.info("Database tables verified and slash commands synced")

    async def close(self) -> None:
        self.poll_pending_assignments.cancel()
        await self.osu_client.close()
        await super().close()

    @tasks.loop(seconds=5.0)
    async def poll_pending_assignments(self) -> None:
        guild = self.get_guild(self.settings.discord_guild_id)
        if guild is None: return
        async with get_db_conn() as db_conn:
            rows = await db_conn.fetch("SELECT id, discord_id, role_id FROM pending_role_assignments WHERE status='pending' LIMIT 20")
            for row in rows:
                try:
                    member = guild.get_member(row["discord_id"]) or await guild.fetch_member(row["discord_id"])
                    role = guild.get_role(row["role_id"])
                    if member and role:
                        await self._replace_digit_roles(member, role)
                        await db_conn.execute("UPDATE pending_role_assignments SET status='done', processed_at=$1 WHERE id=$2", int(time.time()), row["id"])
                except Exception as exc:
                    logger.error(f"Failed to assign role: {exc}")
                    await db_conn.execute("UPDATE pending_role_assignments SET status='failed', processed_at=$1, error_message=$2 WHERE id=$3", int(time.time()), str(exc), row["id"])

    async def _replace_digit_roles(self, member: discord.Member, target_role: discord.Role) -> None:
        role_ids = get_all_digit_role_ids(self.role_mapping)
        old_roles = [role for role in member.roles if role.id in role_ids]
        if old_roles:
            await member.remove_roles(*old_roles, reason="osu! verification update")
        await member.add_roles(target_role, reason="osu! verification success")

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
            await db_conn.execute("INSERT INTO verified_discord_links (discord_id, verified_at) VALUES ($1, $2) ON CONFLICT (discord_id) DO UPDATE SET verified_at=EXCLUDED.verified_at", int(interaction.user.id), int(time.time()))
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
                row = await db_conn.fetchrow("SELECT osu_username FROM users WHERE discord_id=$1", int(interaction.user.id))
                if row: target = row["osu_username"]
                else:
                    await interaction.followup.send("Укажите ник или привяжите профиль.")
                    return

        user = await bot.osu_client.request(f"users/{target}")
        if not user:
            await interaction.followup.send("Игрок не найден.")
            return

        stats = user.get("statistics") or {}
        ambient_color = await _ambient_color_from_avatar(bot, user.get("avatar_url"))
        embed = discord.Embed(title=f"{user.get('country', {}).get('code', '')} {user['username']}", color=ambient_color)
        if user.get("avatar_url"): embed.set_thumbnail(url=user["avatar_url"])

        embed.description = f"**{stats.get('pp', 0):,.2f}pp** • #{stats.get('global_rank', 0):,}"
        embed.add_field(name="Accuracy", value=f"{stats.get('hit_accuracy', 0):.2f}%", inline=True)
        embed.add_field(name="Play time", value=f"{int((stats.get('play_time') or 0) / 3600):,} hrs", inline=True)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="recommend", description="Персональные рекомендации новых карт")
    async def recommend(interaction: discord.Interaction):
        await interaction.response.defer()
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id, osu_username FROM users WHERE discord_id=$1", int(interaction.user.id))
        
        if not row:
            await interaction.followup.send("Сначала привяжите профиль.")
            return
            
        osu_id = row["osu_id"]
        scores, recent = await asyncio.gather(
            bot.osu_client.request(f"users/{osu_id}/scores/best?limit=100"),
            bot.osu_client.request(f"users/{osu_id}/scores/recent?limit=50&include_fails=0")
        )
        
        scores = scores or []
        recent = recent or []
        sample = (recent + scores)[:30]
        avg_stars = await asyncio.gather(*[_star_rating_for_score(bot, s) for s in sample])
        avg_sr = sum(avg_stars) / len(avg_stars) if avg_stars else 4.0

        played_ids = {int(s['beatmap']['id']) for s in (scores + recent) if s.get('beatmap')}
        async with get_db_conn() as db_conn:
            scraped_rows = await db_conn.fetch("SELECT beatmap_id, pp_max FROM scraped_beatmaps ORDER BY RANDOM() LIMIT 150")
        
        # Ускоряем получение данных о картах
        potential_bids = [r['beatmap_id'] for r in scraped_rows if r['beatmap_id'] not in played_ids]
        maps_data = await asyncio.gather(*[bot.osu_client.request(f"beatmaps/{bid}") for bid in potential_bids[:40]])
        
        picks = []
        for bm_data, bid in zip(maps_data, potential_bids):
            if not bm_data or len(picks) >= 5: continue
            map_sr = float(bm_data.get("difficulty_rating") or 0.0)
            pattern = _infer_pattern_label(bm_data)
            if (avg_sr - 0.7) <= map_sr <= (avg_sr + 1.0) or ("LN" in pattern and 6.0 <= map_sr <= 7.5):
                pp_val = next(r['pp_max'] for r in scraped_rows if r['beatmap_id'] == bid)
                picks.append({"bm": bm_data, "sr": map_sr, "pp": pp_val, "pattern": pattern})

        if not picks:
            await interaction.followup.send("Ничего не нашлось. Используйте `/scrape_top`.")
            return

        blocks = []
        for idx, p in enumerate(picks, 1):
            url = f"https://osu.ppy.sh/b/{p['bm']['id']}"
            blocks.append(f"**[{idx}] [{p['bm']['beatmapset']['title']} [{p['bm']['version']}]]({url})**\nSR **{p['sr']:.2f}★** • (~{p['pp']:.0f}PP) • *{p['pattern']}*")

        cover = picks[0]["bm"]["beatmapset"]["covers"]["card@2x"]
        embed = discord.Embed(title=f"Рекомендации для {row['osu_username']}", description=f"Средний SR: {avg_sr:.2f}★\n\n" + "\n\n".join(blocks), color=await _ambient_color_from_image(bot, cover))
        embed.set_image(url=cover)
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="scrape_top", description="Собрать карты из топ-скоров игроков")
    @app_commands.describe(usernames="Ники через запятую", per_user="Сколько скоров (1–100)")
    async def scrape_top(interaction: discord.Interaction, usernames: str, per_user: int = 50) -> None:
        await interaction.response.defer()
        names = _parse_username_list(usernames)
        if not names:
            await interaction.followup.send("Укажи ники.")
            return

        async def fetch_data(name: str):
            user = await bot.osu_client.request(f"users/{quote(name, safe='')}")
            if not user: return None
            scores = await bot.osu_client.request(f"users/{user['id']}/scores/best?limit={per_user}")
            return (user['username'], scores)

        results = await asyncio.gather(*[fetch_data(n) for n in names])
        unique = {}
        summary = []
        for res in results:
            if not res: continue
            uname, scores = res
            summary.append(f"**{uname}** ({len(scores or [])})")
            for s in (scores or []):
                bid = s['beatmap']['id']
                pp = float(s.get('pp') or 0)
                if bid not in unique or pp > unique[bid]['pp_max']:
                    unique[bid] = {'title': f"{s['beatmapset']['title']} [{s['beatmap']['version']}]", 'pp_max': pp, 'url': f"https://osu.ppy.sh/b/{bid}", 'from': uname}

        if unique:
            # ЗАПИСЬ В БАЗУ - ОТКРЫВАЕМ В САМЫЙ ПОСЛЕДНИЙ МОМЕНТ
            to_insert = [(bid, d['title'], d['pp_max']) for bid, d in unique.items()]
            async with get_db_conn() as db_conn:
                await db_conn.executemany("""
                    INSERT INTO scraped_beatmaps (beatmap_id, title, pp_max)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (beatmap_id) DO UPDATE SET pp_max = EXCLUDED.pp_max
                """, to_insert)

            buf = StringIO()
            for bid, d in sorted(unique.items(), key=lambda x: -x[1]['pp_max']):
                buf.write(f"{d['title']}\n{d['url']}\n{d['pp_max']:.0f}pp\n\n")
            
            file = discord.File(fp=BytesIO(buf.getvalue().encode()), filename="scraped.txt")
            await interaction.followup.send(f"Собрано {len(unique)} карт от: {', '.join(summary)}", file=file)
        else:
            await interaction.followup.send("Ничего не найдено.")