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
    """Несколько ников: через запятую/точку с запятой; иначе пробелы."""
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
    """Flatten configured digit role IDs across modes."""
    return {role_id for mode_map in role_mapping.values() for role_id in mode_map.values()}


async def _ambient_color_from_avatar(bot: "RoleBot", avatar_url: str | None) -> discord.Color:
    """Extract a dominant color from avatar image for embed styling."""
    if not avatar_url:
        return discord.Color.blurple()
    try:
        response = await bot.osu_client._http.get(avatar_url)
        response.raise_for_status()
        color = ColorThief(BytesIO(response.content)).get_color(quality=1)
        return discord.Color.from_rgb(*color)
    except Exception:
        return discord.Color.blurple()


async def _ambient_color_from_image(bot: "RoleBot", image_url: str | None) -> discord.Color:
    """Extract dominant color from any image URL."""
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
    """Compact number formatting with K/M suffix for large values."""
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
    """Return accuracy as 0–100, or None if missing."""
    raw = score.get("accuracy")
    if raw is None:
        return None
    try:
        a = float(raw)
    except (TypeError, ValueError):
        return None
    # API may return ratio (0–1) or percent (0–100)
    if a <= 1.0:
        return a * 100.0
    return a


def _ruleset_id_from_beatmap(bm: dict[str, Any]) -> int:
    mi = bm.get("mode_int")
    if mi is not None:
        return int(mi)
    mode = str(bm.get("mode") or "osu")
    return {"osu": 0, "taiko": 1, "fruits": 2, "mania": 3}.get(mode, 0)


def _mods_for_attributes(score: dict[str, Any]) -> list[dict[str, Any]]:
    """Mods as osu! API expects for POST /beatmaps/{id}/attributes (lazer format)."""
    mods_raw = score.get("mods")
    if not mods_raw:
        return []
    if not isinstance(mods_raw, list):
        return []
    out: list[dict[str, Any]] = []
    for m in mods_raw:
        if isinstance(m, dict) and m.get("acronym"):
            item: dict[str, Any] = {"acronym": m["acronym"]}
            if m.get("settings"):
                item["settings"] = m["settings"]
            out.append(item)
    return out


async def _star_rating_for_score(bot: RoleBot, score: dict[str, Any]) -> float:
    """In-game SR for this top play (mods applied), not nomod beatmap.difficulty_rating."""
    bm = score.get("beatmap") or {}
    bid = bm.get("id")
    if bid is None:
        return float(bm.get("difficulty_rating") or 0)
    rid = _ruleset_id_from_beatmap(bm)
    mods = _mods_for_attributes(score)
    got = await bot.osu_client.beatmap_star_rating(int(bid), mods, rid)
    if got is not None:
        return got
    return float(bm.get("difficulty_rating") or 0)


def _infer_pattern_label(bm: dict[str, Any], *, effective_sr: float | None = None) -> str:
    """
    Эвристики по полям osu! API (без .osu): LN, темп, плотность, длина, SR.

    Примеры тегов: Full LN / Heavy LN / Hybrid / Rice; Stamina (5+ мин); Speed (BPM >= 270);
    Speedjack; Chordjack; Dance / Dance stream / Dance jack; Handstream; Jumpstream;
    Stream; Finger control; Mid-high; Control; Reading / Tech (высокий SR при низкой плотности).
    """
    circles = int(bm.get("count_circles") or 0)
    sliders = int(bm.get("count_sliders") or 0)
    spinners = int(bm.get("count_spinners") or 0)
    objects = circles + sliders + spinners
    total = max(objects, 1)
    slider_ratio = sliders / total
    bpm = float(bm.get("bpm") or 0)

    length_sec = float(bm.get("hit_length") or bm.get("total_length") or 0)
    if length_sec <= 0:
        length_sec = max(float(bm.get("total_length") or 1), 1.0)
    nps = objects / max(length_sec, 1.0)
    sr = float(effective_sr) if effective_sr is not None else float(bm.get("difficulty_rating") or 0.0)

    # LN-слой (доля слайдеров как прокси LN)
    if slider_ratio > 0.61:
        ln_label = "Full LN"
    elif slider_ratio >= 0.35:
        ln_label = "Heavy LN"
    elif slider_ratio >= 0.18:
        ln_label = "Hybrid"
    else:
        ln_label = "Rice"

    tags: list[str] = []

    if length_sec >= 300:
        tags.append("Stamina")

    # Основной «геймплейный» тег (взаимоисключающие ветки по приоритету)
    primary: str | None = None
    riceish = slider_ratio < 0.32
    streamish = 0.22 <= slider_ratio <= 0.52

    if bpm >= 270:
        primary = "Speed"
    elif 240 <= bpm < 270 and nps >= 11.0 and slider_ratio <= 0.48:
        primary = "Speedjack"
    elif 155 <= bpm <= 205:
        if nps >= 10.5 and riceish:
            primary = "Dance jack"
        elif nps >= 7.0:
            primary = "Dance stream"
        else:
            primary = "Dance"
    elif 175 <= bpm < 265 and nps >= 10.0 and riceish:
        primary = "Chordjack"
    elif 185 <= bpm < 240 and streamish and nps >= 8.0:
        primary = "Handstream"
    elif 170 <= bpm < 235 and 8.0 <= nps < 12.5 and riceish:
        primary = "Jumpstream"
    elif bpm >= 220 and nps >= 9.0:
        primary = "Stream"
    elif bpm >= 195:
        primary = "Mid-high"
    elif bpm >= 170:
        primary = "Finger control"
    else:
        primary = "Control"

    if primary:
        tags.append(primary)

    # Высокий SR при относительно низкой плотности — упор на чтение / технику
    if sr >= 6.2 and nps < 6.5:
        tags.append("Reading")
    elif sr >= 5.8 and nps < 7.5 and slider_ratio < 0.4:
        tags.append("Tech")

    # Доп. тег, если не пересекается по смыслу (Chordjack + не дублировать Dance)
    extra: str | None = None
    if primary not in {"Chordjack", "Dance", "Dance jack", "Dance stream"}:
        if 175 <= bpm <= 265 and nps >= 10.5 and riceish and primary != "Chordjack":
            extra = "Chordjack"
    if extra and extra not in tags:
        tags.append(extra)

    # Сборка: LN + уникальные теги
    out: list[str] = [ln_label]
    seen: set[str] = {ln_label}
    for t in tags:
        if t not in seen:
            out.append(t)
            seen.add(t)
    return " / ".join(out)

class RoleBot(commands.Bot):
    """Discord bot with slash commands and assignment worker."""

    def __init__(self, *, settings: Settings, osu_client: OsuClient, role_mapping: dict[str, dict[int, int]]) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True  # Нужно для текстовой команды !sync
        super().__init__(command_prefix="!", intents=intents)
        self.settings = settings
        self.osu_client = osu_client
        self.role_mapping = role_mapping

    async def setup_hook(self) -> None:
        # Запуск фоновой задачи на выдачу ролей
        self.poll_pending_assignments.start()
        # Авто-синхронизация при старте
        await self.tree.sync()
        logger.info("Slash commands synced")

    async def close(self) -> None:
        self.poll_pending_assignments.cancel()
        await self.osu_client.close()
        await super().close()

    @tasks.loop(seconds=5.0)
    async def poll_pending_assignments(self) -> None:
        """Очередь выдачи ролей после подтверждения на сайте."""
        guild = self.get_guild(self.settings.discord_guild_id)
        if guild is None:
            return
        async with get_db_conn() as db_conn:
            rows = await db_conn.fetch(
                """
                SELECT id, discord_id, role_id, osu_id, osu_username, mode, digit_value
                FROM pending_role_assignments
                WHERE status='pending'
                ORDER BY id ASC LIMIT 20
                """
            )
            
            for row in rows:
                assignment_id, discord_id, role_id, osu_id, osu_username, mode, digit = row
                try:
                    member = guild.get_member(discord_id) or await guild.fetch_member(discord_id)
                    role = guild.get_role(role_id)
                    if member and role:
                        await self._replace_digit_roles(member, role)
                        await db_conn.execute(
                            "UPDATE pending_role_assignments SET status='done', processed_at=$1 WHERE id=$2",
                            int(time.time()),
                            assignment_id,
                        )
                except Exception as exc:
                    logger.error(f"Failed to assign role: {exc}")
                    await db_conn.execute(
                        "UPDATE pending_role_assignments SET status='failed', processed_at=$1, error_message=$2 WHERE id=$3",
                        int(time.time()),
                        str(exc),
                        assignment_id,
                    )

    async def _replace_digit_roles(self, member: discord.Member, target_role: discord.Role) -> None:
        role_ids = get_all_digit_role_ids(self.role_mapping)
        old_roles = [role for role in member.roles if role.id in role_ids]
        if old_roles:
            await member.remove_roles(*old_roles, reason="osu! verification update")
        await member.add_roles(target_role, reason="osu! verification success")

def register_commands(bot: RoleBot) -> None:
    """Регистрация команд: верификация, профиль и динамические рекомендации."""

    # --- СИСТЕМНЫЕ КОМАНДЫ ---

    @bot.tree.command(name="linkcode", description="Связать Discord с профилем через код с сайта")
    async def linkcode(interaction: discord.Interaction, code: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=False)
        code = code.strip().upper()
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow(
                "SELECT osu_username, discord_id FROM verification_challenges WHERE link_code = $1 AND status = 'pending'",
                code,
            )

            if row is None:
                await interaction.followup.send(f"❌ Код `{code}` не найден.", ephemeral=True)
                return

            osu_name, expected_discord_id = row["osu_username"], row["discord_id"]
            if expected_discord_id and int(expected_discord_id) != int(interaction.user.id):
                await interaction.followup.send("❌ Этот код выдан другому Discord ID.", ephemeral=True)
                return
            await db_conn.execute(
                """
                INSERT INTO verified_discord_links (discord_id, verified_at)
                VALUES ($1, $2)
                ON CONFLICT (discord_id) DO UPDATE SET verified_at=EXCLUDED.verified_at
                """,
                int(interaction.user.id),
                int(time.time()),
            )
        await interaction.followup.send(f"✅ Аккаунт **{osu_name}** привязан!", ephemeral=True)

    @bot.command(name="sync")
    @commands.is_owner()
    async def sync(ctx: commands.Context):
        await bot.tree.sync()
        await ctx.send("✅ Команды синхронизированы!")

    # --- ПРОФИЛЬ ---

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
        avatar_url = user.get("avatar_url")
        ambient_color = await _ambient_color_from_avatar(bot, avatar_url)

        country = (user.get("country") or {}).get("code", "")
        title = f"{country} {user['username']}".strip()
        embed = discord.Embed(title=title, color=ambient_color)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        global_rank = stats.get("global_rank")
        country_rank = stats.get("country_rank")
        pp = stats.get("pp", 0)
        level = (stats.get("level") or {}).get("current", 0)
        hit_acc = stats.get("hit_accuracy", 0)
        play_count = stats.get("play_count", 0)
        play_time_h = int((stats.get("play_time") or 0) / 3600)

        embed.description = (
            f"**{pp:,.2f}pp**"
            + (f" • #{global_rank:,}" if global_rank else "")
            + (f" • {country}#{country_rank:,}" if country_rank and country else "")
        )
        embed.add_field(name="Accuracy", value=f"{hit_acc:.2f}%", inline=True)
        embed.add_field(name="Level", value=f"{level}", inline=True)
        embed.add_field(name="Play count", value=f"{play_count:,}", inline=True)
        embed.add_field(name="Total hits", value=f"{stats.get('total_hits', 0):,}", inline=True)
        embed.add_field(name="Max combo", value=f"{stats.get('maximum_combo', 0):,}", inline=True)
        embed.add_field(name="Play time", value=f"{play_time_h:,} hrs", inline=True)
        embed.add_field(name="Grade SSH", value=f"{(stats.get('grade_counts') or {}).get('ssh', 0):,}", inline=True)
        embed.add_field(name="Grade SS", value=f"{(stats.get('grade_counts') or {}).get('ss', 0):,}", inline=True)
        embed.add_field(name="Grade S", value=f"{(stats.get('grade_counts') or {}).get('s', 0):,}", inline=True)
        if user.get("join_date"):
            embed.set_footer(text=f"Joined osu!: {user['join_date'][:10]}")

        await interaction.followup.send(embed=embed)

    # --- РЕКОМЕНДАЦИИ (БЕЗ НОВОЙ ТАБЛИЦЫ) ---

    @bot.tree.command(name="recommend", description="Персональные рекомендации новых карт на основе вашего уровня")
    async def recommend(interaction: discord.Interaction):
        """Рекомендует новые карты из базы данных, которые игрок еще не играл."""
        try:
            await interaction.response.defer(ephemeral=False, thinking=False)
        except discord.NotFound:
            return
        
        # 1. Получаем данные игрока
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id, osu_username FROM users WHERE discord_id=$1", int(interaction.user.id))
        
        if not row:
            await interaction.followup.send("Сначала привяжите профиль через `/setprofile` или сайт.")
            return
            
        osu_id, username = row["osu_id"], row["osu_username"]
        
        # 2. Анализируем текущий скилл (Средний SR по Top + Recent)
        scores = await bot.osu_client.request(f"users/{osu_id}/scores/best?limit=100") or []
        recent_raw = await bot.osu_client.request(f"users/{osu_id}/scores/recent?limit=50&include_fails=0") or []

        async def is_valid_score(s: dict[str, Any]) -> bool:
            acc = _score_accuracy_percent(s)
            if acc is None or acc > 96.0: return False
            mods = s.get("mods", [])
            has_dc = any(isinstance(m, dict) and m.get("acronym") == "DC" for m in mods)
            if has_dc:
                bm = s.get("beatmap") or {}
                if float(bm.get("difficulty_rating") or 0.0) >= 6.0: return False
            return True

        valid_top = [s for s in scores if await is_valid_score(s)]
        recent_filtered = [s for s in recent_raw if await is_valid_score(s)]

        # Смешиваем для расчета среднего SR
        r_sample = random.sample(recent_filtered, min(20, len(recent_filtered)))
        t_sample = random.sample(valid_top, min(30 - len(r_sample), len(valid_top)))
        sample_for_avg = r_sample + t_sample
        
        # Если играть в "чистое" (с высокой аккураси) нечего, берем последние 20 хоть каких-то игр
        if not sample_for_avg: sample_for_avg = (recent_raw + scores)[:20]

        avg_stars = await asyncio.gather(*[_star_rating_for_score(bot, s) for s in sample_for_avg])
        avg_sr = sum(avg_stars) / len(avg_stars) if avg_stars else 0.0

        # 3. Поиск НОВЫХ карт в базе данных
        played_ids = {int(s['beatmap']['id']) for s in (scores + recent_raw) if s.get('beatmap')}

        async with get_db_conn() as db_conn:
            # Достаем случайный пак из базы
            scraped_rows = await db_conn.fetch("SELECT beatmap_id, pp_max FROM scraped_beatmaps ORDER BY RANDOM() LIMIT 250")
        
        picks: list[dict[str, Any]] = []
        pick_stars: list[float] = []

        for r in scraped_rows:
            if len(picks) >= 5: break
            bid = r['beatmap_id']
            if bid in played_ids: continue
                
            bm_data = await bot.osu_client.request(f"beatmaps/{bid}")
            if not bm_data: continue
            
            map_sr = float(bm_data.get("difficulty_rating") or 0.0)
            pattern = _infer_pattern_label(bm_data)
            is_ln = "LN" in pattern
            
            # Логика фильтрации: В пределах SR или исключение для 6*+ LN
            in_range = (avg_sr - 1.0) <= map_sr <= (avg_sr + 0.8)
            ln_bonus = (is_ln and 6.0 <= map_sr <= 7.5)

            if in_range or ln_bonus:
                picks.append({
                    "beatmap": bm_data,
                    "beatmapset": bm_data.get("beatmapset"),
                    "pp": r['pp_max']
                })
                pick_stars.append(map_sr)

        if not picks:
            await interaction.followup.send(f"Подходящих новых карт не найдено. Наполните базу командой `/scrape_top`.")
            return

        # 4. Отрисовка результата
        blocks: list[str] = []
        for idx, (p, s_stars) in enumerate(zip(picks, pick_stars), start=1):
            bm = p["beatmap"]
            bset = p["beatmapset"] or {}
            pattern = _infer_pattern_label(bm, effective_sr=s_stars)
            url = f"https://osu.ppy.sh/b/{bm.get('id')}"
            blocks.append(
                f"**[{idx}] [{bset.get('title')} [{bm.get('version')}]]({url})**\n"
                f"SR **{s_stars:.2f}★** • (~{p['pp']:.0f}PP) • BPM {_short_num(bm.get('bpm'), 0)}\n"
                f"▶ Objects {bm.get('count_circles')} • Sliders {bm.get('count_sliders')}\n"
                f"▶ Pattern *{pattern}*"
            )

        cover = (picks[0]["beatmapset"] or {}).get("covers", {}).get("card@2x")
        ambient_color = await _ambient_color_from_image(bot, cover)

        embed = discord.Embed(
            title=f"Персональные рекомендации для {username}",
            description=f"**Ваш средний SR: {avg_sr:.2f}★**\n*(Исключены сыгранные карты)*\n\n" + "\n\n".join(blocks),
            color=ambient_color,
        )
        if cover: embed.set_image(url=cover)

        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="top_list", description="Показать ваши лучшие карты")
    async def top_list(interaction: discord.Interaction):
        """Просто показывает список ваших лучших карт без сохранения в базу."""
        await interaction.response.defer()
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id FROM users WHERE discord_id=$1", int(interaction.user.id))
        
        if not row:
            await interaction.followup.send("Профиль не привязан.")
            return

        scores = await bot.osu_client.request(f"users/{row['osu_id']}/scores/best?limit=5")
        if not scores:
            await interaction.followup.send("Ошибочка при получении топа.")
            return

        text = "\n".join([f"**{i+1}.** {s['beatmapset']['title']} [{s['beatmap']['version']}] — {s['pp']}pp" for i, s in enumerate(scores)])
        await interaction.followup.send(f"🏆 **Ваш Топ-5:**\n{text}")

    @bot.tree.command(
        name="scrape_top",
        description="Собрать карты из топ-скоров указанных игроков (несколько ников)",
    )
    @app_commands.describe(
        usernames="Ники через запятую или пробел: mitix, player2 или mitix player2",
        per_user="Сколько лучших скоров на игрока (1–100)",
    )
    async def scrape_top(
        interaction: discord.Interaction,
        usernames: str,
        per_user: int = 50,
    ) -> None:
        await interaction.response.defer(ephemeral=False, thinking=False)
        names = _parse_username_list(usernames)
        if not names:
            await interaction.followup.send("Укажи хотя бы один ник.")
            return
        try:
            cap = max(1, min(100, int(per_user)))
        except (TypeError, ValueError):
            cap = 50

        async def fetch_profile_top(name: str) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]] | None, str | None]:
            safe = quote(name, safe="")
            user = await bot.osu_client.request(f"users/{safe}")
            if not user:
                return name, None, None, f"`{name}` не найден"
            uid = int(user["id"])
            scores = await bot.osu_client.request(f"users/{uid}/scores/best?limit={cap}")
            if not scores:
                return name, user, [], f"`{name}` — топ пуст"
            return name, user, scores, None

        rows = await asyncio.gather(*[fetch_profile_top(n) for n in names])

        unique: dict[int, dict[str, Any]] = {}
        per_user_summary: list[str] = []
        errors: list[str] = []

        for name, user, scores, err in rows:
            if err:
                errors.append(err)
                continue
            assert user is not None and scores is not None
            uname = str(user.get("username", name))
            per_user_summary.append(f"**{uname}** — {len(scores)} скоров")
            for score in scores:
                bm = score.get("beatmap") or {}
                bset = score.get("beatmapset") or {}
                bid = bm.get("id")
                if bid is None:
                    continue
                bid = int(bid)
                title = f"{bset.get('title', '?')} [{bm.get('version', '?')}]"
                url = f"https://osu.ppy.sh/b/{bid}"
                pp = score.get("pp")
                if bid not in unique:
                    unique[bid] = {"title": title, "url": url, "pp_max": float(pp or 0), "from": set()}
                unique[bid]["from"].add(uname)
                if pp is not None and float(pp) > unique[bid]["pp_max"]:
                    unique[bid]["pp_max"] = float(pp)

        lines = [
            f"Игроков: **{len(names)}** · скоров на игрока: **{cap}** · уникальных карт: **{len(unique)}**",
        ]
        if errors:
            lines.append("Ошибки: " + " · ".join(errors))
        lines.append("")
        lines.extend(per_user_summary)

        embed = discord.Embed(
            title="Топ-карты по профилям",
            description="\n".join(lines)[:4096],
            color=discord.Color.blurple(),
        )

        preview_lines: list[str] = []
        for bid, data in sorted(unique.items(), key=lambda x: -x[1]["pp_max"])[:15]:
            who = ", ".join(sorted(data["from"]))
            preview_lines.append(
                f"• [{data['title']}]({data['url']}) — {data['pp_max']:.0f}pp ({who})"
            )
        if preview_lines:
            embed.add_field(
                name="Топ-15 по PP (уникальные)",
                value="\n".join(preview_lines)[:1024],
                inline=False,
            )

        files: list[discord.File] = []
        if unique:
            # 1. Открываем соединение ОДИН раз
            async with get_db_conn() as db_conn:
                
                # 2. ПОДГОТОВКА ДАННЫХ: Собираем все карты в один список кортежей
                to_insert = [
                    (bid, data['title'], data['pp_max']) 
                    for bid, data in unique.items()
                ]

                # 3. МАССОВАЯ ВСТАВКА: Одной командой закидываем всё в базу
                # Это в десятки раз быстрее и не вызывает таймаутов
                await db_conn.executemany("""
                    INSERT INTO scraped_beatmaps (beatmap_id, title, pp_max)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (beatmap_id) DO UPDATE SET pp_max = EXCLUDED.pp_max
                """, to_insert)

                # 4. ФОРМИРОВАНИЕ ФАЙЛА: Теперь спокойно пишем в буфер для лога
                buf = StringIO()
                for bid, data in sorted(unique.items(), key=lambda x: -x[1]["pp_max"]):
                    who = ", ".join(sorted(data["from"]))
                    buf.write(f"{data['title']}\n{data['url']}\n{data['pp_max']:.0f}pp · {who}\n\n")
            
            # Создаем файл для отправки в Discord
            files.append(
                discord.File(
                    fp=BytesIO(buf.getvalue().encode("utf-8")),
                    filename="scrape_top_maps.txt",
                )
            )

        if files:
            await interaction.followup.send(embed=embed, files=files)
        else:
            await interaction.followup.send(embed=embed)