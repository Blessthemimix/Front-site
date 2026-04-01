"""SQLite storage and repository helpers."""

from __future__ import annotations

from pathlib import Path

import aiosqlite


async def init_db(database_path: str) -> None:
    """Create required tables."""
    db_file = Path(database_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(database_path) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                osu_username TEXT NOT NULL,
                osu_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS discord_link_codes (
                discord_id INTEGER PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verified_discord_links (
                discord_id INTEGER PRIMARY KEY,
                verified_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS osu_claims (
                osu_id INTEGER PRIMARY KEY,
                discord_id INTEGER NOT NULL,
                claimed_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_challenges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                osu_id INTEGER NOT NULL,
                osu_username TEXT NOT NULL,
                mode TEXT NOT NULL,
                profile_token TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_role_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_id INTEGER NOT NULL,
                osu_id INTEGER NOT NULL,
                osu_username TEXT NOT NULL,
                mode TEXT NOT NULL,
                digit_value INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at INTEGER NOT NULL,
                processed_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS maps (
                map_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                sr REAL NOT NULL,
                mode TEXT NOT NULL,
                image_url TEXT,
                bpm REAL,
                key_count INTEGER,
                circles INTEGER,
                sliders INTEGER,
                length INTEGER
            );
            CREATE TABLE IF NOT EXISTS oauth_osu_states (
                state TEXT PRIMARY KEY,
                discord_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL
            );
            """)
        await _migrate_schema(db)
        await db.commit()


async def _migrate_schema(db: aiosqlite.Connection) -> None:
    """Add columns/tables for older deployments."""
    async with db.execute("PRAGMA table_info(verification_challenges)") as cursor:
        cols = {row[1] for row in await cursor.fetchall()}
    if "verification_source" not in cols:
        await db.execute(
            "ALTER TABLE verification_challenges ADD COLUMN verification_source TEXT NOT NULL DEFAULT 'bio'"
        )
