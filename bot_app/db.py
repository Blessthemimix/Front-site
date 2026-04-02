import os
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

# Добавь эту проверку для отладки:
if not DATABASE_URL:
    print("КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не найдена в окружении!")
else:
    print(f"DATABASE_URL загружена, начинается на: {DATABASE_URL[:15]}...")

async def get_db_conn():

async def init_db():
    """Создает все необходимые таблицы в Supabase."""
    conn = await get_db_conn()
    try:
        # В PostgreSQL вместо INTEGER PRIMARY KEY AUTOINCREMENT используется SERIAL PRIMARY KEY
        # Вместо INTEGER для Discord ID используем BIGINT (это критично!)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id BIGINT PRIMARY KEY,
                osu_username TEXT NOT NULL,
                osu_id BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discord_link_codes (
                discord_id BIGINT PRIMARY KEY,
                code TEXT NOT NULL,
                expires_at BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS verified_discord_links (
                discord_id BIGINT PRIMARY KEY,
                verified_at BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS osu_claims (
                osu_id BIGINT PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                claimed_at BIGINT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS verification_challenges (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                osu_id BIGINT NOT NULL,
                osu_username TEXT NOT NULL,
                mode TEXT NOT NULL,
                profile_token TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL,
                verification_source TEXT NOT NULL DEFAULT 'bio',
                link_code TEXT
            );

            CREATE TABLE IF NOT EXISTS pending_role_assignments (
                id SERIAL PRIMARY KEY,
                discord_id BIGINT NOT NULL,
                osu_id BIGINT NOT NULL,
                osu_username TEXT NOT NULL,
                mode TEXT NOT NULL,
                digit_value INTEGER NOT NULL,
                role_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error_message TEXT,
                created_at BIGINT NOT NULL,
                processed_at BIGINT
            );

            CREATE TABLE IF NOT EXISTS maps (
                map_id BIGINT PRIMARY KEY,
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
                discord_id BIGINT NOT NULL,
                created_at BIGINT NOT NULL,
                expires_at BIGINT NOT NULL
            );
        """)
        print("База данных Supabase успешно инициализирована.")
    finally:
        await conn.close()