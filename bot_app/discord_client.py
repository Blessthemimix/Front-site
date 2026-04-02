"""Discord bot implementation and role assignment worker."""

from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any

import discord
from colorthief import ColorThief
from discord.ext import commands, tasks
from discord import app_commands

from .config import Settings
from .db import get_db_conn
from .osu_client import OsuClient
from .verification import VerificationInput, compute_digit_value

logger = logging.getLogger(__name__)

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

    @bot.tree.command(name="recommend", description="Несколько рекомендаций из вашего топ-100")
    async def recommend(interaction: discord.Interaction):
        """Показывает несколько случайных карт из топ-100 игрока через API."""
        await interaction.response.defer()
        
        # Берем привязанный ID из твоей таблицы users
        async with get_db_conn() as db_conn:
            row = await db_conn.fetchrow("SELECT osu_id, osu_username FROM users WHERE discord_id=$1", int(interaction.user.id))
        
        if not row:
            await interaction.followup.send("Сначала привяжите профиль через `/setprofile` или сайт.")
            return
            
        osu_id, username = row["osu_id"], row["osu_username"]
        
        # Запрос в osu! API напрямую (в логах видно, что это работает)
        scores = await bot.osu_client.request(f"users/{osu_id}/scores/best?limit=100")
        if not scores:
            await interaction.followup.send("Не удалось загрузить ваши топ-плеи.")
            return

        import random
        picks_count = min(5, len(scores))
        picks = random.sample(scores, picks_count)

        lines: list[str] = []
        for idx, score in enumerate(picks, start=1):
            bm = score.get("beatmap") or {}
            bset = score.get("beatmapset") or {}
            title = bset.get("title", "Unknown map")
            version = bm.get("version", "?")
            sr = bm.get("difficulty_rating", "?")
            bpm = bm.get("bpm", "?")
            pp = score.get("pp", "?")
            url = f"https://osu.ppy.sh/b/{bm.get('id')}" if bm.get("id") else "https://osu.ppy.sh/"
            lines.append(
                f"**{idx}. [{title} [{version}]]({url})**\n"
                f"⭐ {sr} • {pp}pp • BPM {bpm}"
            )

        embed = discord.Embed(
            title=f"Персональные рекомендации для {username}",
            description="\n\n".join(lines),
            color=0x3498DB,
        )
        cover = (picks[0].get("beatmapset") or {}).get("covers", {}).get("list@2x")
        if cover:
            embed.set_thumbnail(url=cover)

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