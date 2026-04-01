"""SQLite storage and repository helpers."""

from __future__ import annotations

import aiosqlite


async def init_db(database_path: str) -> None:
    """Create required tables."""
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
            """)
        await db.commit()
