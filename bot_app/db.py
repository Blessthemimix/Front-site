import os
import asyncpg
from dotenv import load_dotenv
from contextlib import asynccontextmanager # 1. Добавляем этот импорт

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    print("КРИТИЧЕСКАЯ ОШИБКА: Переменная DATABASE_URL не найдена в окружении!")
else:
    print(f"DATABASE_URL загружена, начинается на: {DATABASE_URL[:15]}...")

# 2. Добавляем декоратор и аргументы в скобки
@asynccontextmanager
async def get_db_conn(url: str = None, key: str = None):
    """Создает подключение к PostgreSQL и автоматически закрывает его."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn # 3. Используем yield вместо return
    finally:
        await conn.close() # Автоматическое закрытие

async def init_db():
    """Создает все необходимые таблицы в Supabase."""
    # 4. Здесь тоже меняем вызов на async with
    async with get_db_conn() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                discord_id BIGINT PRIMARY KEY,
                osu_username TEXT NOT NULL,
                osu_id BIGINT NOT NULL
            );
            -- ... (весь остальной твой SQL код без изменений) ...
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