"""Discord bot implementation and role assignment worker."""

from __future__ import annotations

import logging
import time
from io import BytesIO
from typing import Any

import aiosqlite
import discord
from colorthief import ColorThief
from discord.ext import commands, tasks
from discord import app_commands

from .config import Settings
from .osu_client import OsuClient
from .verification import VerificationInput, compute_digit_value

logger = logging.getLogger(__name__)

def get_all_digit_role_ids(role_mapping: dict[str, dict[int, int]]) -> set[int]:
    """Flatten configured digit role IDs across modes."""
    return {role_id for mode_map in role_mapping.values() for role_id in mode_map.values()}

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
        async with aiosqlite.connect(self.settings.database_path) as db_conn:
            async with db_conn.execute("""
                SELECT id, discord_id, role_id, osu_id, osu_username, mode, digit_value
                FROM pending_role_assignments
                WHERE status='pending'
                ORDER BY id ASC LIMIT 20
                """) as cursor:
                rows = await cursor.fetchall()
            
            for row in rows:
                assignment_id, discord_id, role_id, osu_id, osu_username, mode, digit = row
                try:
                    member = guild.get_member(discord_id) or await guild.fetch_member(discord_id)
                    role = guild.get_role(role_id)
                    if member and role:
                        await self._replace_digit_roles(member, role)
                        await db_conn.execute(
                            "UPDATE pending_role_assignments SET status='done', processed_at=? WHERE id=?",
                            (int(time.time()), assignment_id),
                        )
                except Exception as exc:
                    logger.error(f"Failed to assign role: {exc}")
                    await db_conn.execute(
                        "UPDATE pending_role_assignments SET status='failed', processed_at=?, error_message=? WHERE id=?",
                        (int(time.time()), str(exc), assignment_id),
                    )
            await db_conn.commit()

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
        code = code.strip().upper()
        async with aiosqlite.connect(bot.settings.database_path) as db_conn:
            async with db_conn.execute(
                "SELECT id, osu_username FROM verification_challenges WHERE link_code = ? AND status = 'pending'", 
                (code,)
            ) as cursor:
                row = await cursor.fetchone()

            if row is None:
                await interaction.response.send_message(f"❌ Код `{code}` не найден.", ephemeral=True)
                return

            challenge_id, osu_name = row
            await db_conn.execute("UPDATE verification_challenges SET discord_id = ? WHERE id = ?", (interaction.user.id, challenge_id))
            await db_conn.commit()
        await interaction.response.send_message(f"✅ Аккаунт **{osu_name}** привязан!", ephemeral=True)

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
            async with aiosqlite.connect(bot.settings.database_path) as db_conn:
                async with db_conn.execute("SELECT osu_username FROM users WHERE discord_id=?", (interaction.user.id,)) as cursor:
                    row = await cursor.fetchone()
            if row: target = row[0]
            else:
                await interaction.followup.send("Укажите ник или привяжите профиль.")
                return

        user = await bot.osu_client.request(f"users/{target}")
        if not user:
            await interaction.followup.send("Игрок не найден.")
            return
        
        embed = discord.Embed(title=f"Профиль {user['username']}", color=0xFF66AA)
        embed.set_thumbnail(url=user['avatar_url'])
        stats = user.get('statistics', {})
        embed.add_field(name="Ранг", value=f"#{stats.get('global_rank', 0) or 0:,}")
        embed.add_field(name="PP", value=f"{stats.get('pp', 0):,}")
        await interaction.followup.send(embed=embed)

    # --- РЕКОМЕНДАЦИИ (БЕЗ НОВОЙ ТАБЛИЦЫ) ---

    @bot.tree.command(name="recommend", description="Случайная карта из вашего топ-100")
    async def recommend(interaction: discord.Interaction):
        """Выбирает случайную карту из топ-100 игрока через API."""
        await interaction.response.defer()
        
        # Берем привязанный ID из твоей таблицы users
        async with aiosqlite.connect(bot.settings.database_path) as db_conn:
            async with db_conn.execute("SELECT osu_id, osu_username FROM users WHERE discord_id=?", (interaction.user.id,)) as cursor:
                row = await cursor.fetchone()
        
        if not row:
            await interaction.followup.send("Сначала привяжите профиль через `/setprofile` или сайт.")
            return
            
        osu_id, username = row
        
        # Запрос в osu! API напрямую (в логах видно, что это работает)
        scores = await bot.osu_client.request(f"users/{osu_id}/scores/best?limit=100")
        if not scores:
            await interaction.followup.send("Не удалось загрузить ваши топ-плеи.")
            return

        import random
        score = random.choice(scores)
        bm = score['beatmap']
        bset = score['beatmapset']
        
        embed = discord.Embed(
            title=f"Рекомендация для {username}",
            description=f"Как насчет перепройти **{bset['title']}**?",
            url=f"https://osu.ppy.sh/b/{bm['id']}",
            color=0x3498DB
        )
        embed.add_field(name="Сложность", value=f"[{bm['version']}]")
        embed.add_field(name="SR", value=f"{bm['difficulty_rating']}⭐")
        embed.set_thumbnail(url=bset['covers']['list@2x'])
        
        await interaction.followup.send(embed=embed)

    @bot.tree.command(name="top_list", description="Показать ваши лучшие карты")
    async def top_list(interaction: discord.Interaction):
        """Просто показывает список ваших лучших карт без сохранения в базу."""
        await interaction.response.defer()
        async with aiosqlite.connect(bot.settings.database_path) as db_conn:
            async with db_conn.execute("SELECT osu_id FROM users WHERE discord_id=?", (interaction.user.id,)) as cursor:
                row = await cursor.fetchone()
        
        if not row:
            await interaction.followup.send("Профиль не привязан.")
            return

        scores = await bot.osu_client.request(f"users/{row[0]}/scores/best?limit=5")
        if not scores:
            await interaction.followup.send("Ошибочка при получении топа.")
            return

        text = "\n".join([f"**{i+1}.** {s['beatmapset']['title']} [{s['beatmap']['version']}] — {s['pp']}pp" for i, s in enumerate(scores)])
        await interaction.followup.send(f"🏆 **Ваш Топ-5:**\n{text}")