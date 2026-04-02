import asyncio
import logging
import os
import socket
from urllib.parse import urlparse, parse_qs, unquote
import asyncpg
from dotenv import load_dotenv
from contextlib import asynccontextmanager

load_dotenv()
logger = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

@asynccontextmanager
async def get_db_conn(url: str = None, key: str = None):
    """
    Создает подключение к PostgreSQL. 
    Аргументы url и key добавлены для совместимости с вызовами из других модулей.
    """
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL is not configured")
    # Supabase pooler (pgbouncer in transaction mode) is incompatible with
    # asyncpg prepared statement cache. Disable cache for stable connections.
    def _parse_dsn(dsn: str) -> tuple[str, int, str, str, str, bool]:
        p = urlparse(dsn)
        host = p.hostname or ""
        port = int(p.port or 5432)
        user = unquote(p.username or "")
        password = unquote(p.password or "")
        dbname = (p.path or "").lstrip("/") or "postgres"
        q = parse_qs(p.query or "")
        sslmode = (q.get("sslmode", [""])[0] or "").lower()
        ssl_required = sslmode == "require"
        return host, port, user, password, dbname, ssl_required

    async def _connect_ipv4_fallback(dsn: str) -> asyncpg.Connection:
        host, port, user, password, dbname, ssl_required = _parse_dsn(dsn)
        infos = socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        ip = infos[0][4][0]
        ssl = "require" if ssl_required else None
        return await asyncpg.connect(
            host=ip,
            port=port,
            user=user,
            password=password,
            database=dbname,
            ssl=ssl,
            statement_cache_size=0,
            command_timeout=60,
            timeout=20,
        )

    for attempt in range(1, 6):
        try:
            conn = await asyncpg.connect(
                DATABASE_URL,
                statement_cache_size=0,
                command_timeout=60,
                timeout=20,
            )
            break
        except Exception as exc:  # noqa: BLE001
            # On some hosts IPv6 route can be unavailable; fall back to IPv4.
            if isinstance(exc, OSError) and getattr(exc, "errno", None) == 101:
                try:
                    conn = await _connect_ipv4_fallback(DATABASE_URL)
                    break
                except Exception as exc2:  # noqa: BLE001
                    exc = exc2
            wait_s = min(2 * attempt, 10)
            logger.warning("DB connect failed (attempt %s/5): %s", attempt, exc)
            if attempt == 5:
                raise
            await asyncio.sleep(wait_s)
    try:
        yield conn
    finally:
        try:
            await conn.close()
        except OSError as exc:
            # Windows can intermittently throw WinError 121 while closing pooled/network sockets.
            # Connection is already unusable at this point, so we don't fail caller logic on close.
            logger.warning("DB connection close warning: %s", exc)

async def init_db():
    """Создает все необходимые таблицы в Supabase."""
    async with get_db_conn() as conn:
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
        # Backward-compatible migrations for existing deployments.
        await _ensure_column(conn, "verification_challenges", "mode", "TEXT NOT NULL DEFAULT 'osu'")
        await _ensure_column(conn, "verification_challenges", "profile_token", "TEXT NOT NULL DEFAULT ''")
        await _ensure_column(conn, "verification_challenges", "status", "TEXT NOT NULL DEFAULT 'pending'")
        await _ensure_column(conn, "verification_challenges", "created_at", "BIGINT NOT NULL DEFAULT 0")
        await _ensure_column(conn, "verification_challenges", "expires_at", "BIGINT NOT NULL DEFAULT 0")
        await _ensure_column(
            conn,
            "verification_challenges",
            "verification_source",
            "TEXT NOT NULL DEFAULT 'bio'",
        )
        await _ensure_column(conn, "verification_challenges", "link_code", "TEXT")
        await _ensure_identity_pk(conn, "verification_challenges")

        await _ensure_column(conn, "pending_role_assignments", "status", "TEXT NOT NULL DEFAULT 'pending'")
        await _ensure_column(conn, "pending_role_assignments", "error_message", "TEXT")
        await _ensure_column(conn, "pending_role_assignments", "created_at", "BIGINT NOT NULL DEFAULT 0")
        await _ensure_column(conn, "pending_role_assignments", "processed_at", "BIGINT")
        await _ensure_identity_pk(conn, "pending_role_assignments")
        print("База данных Supabase успешно инициализирована.")


async def _ensure_column(conn: asyncpg.Connection, table: str, column: str, spec: str) -> None:
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = $1 AND column_name = $2
        )
        """,
        table,
        column,
    )
    if not exists:
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


async def _ensure_identity_pk(conn: asyncpg.Connection, table: str) -> None:
    """
    Ensure legacy tables have numeric `id` with generated values.
    Some early schemas were created without `id`, while app code expects it.
    """
    has_id = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = $1 AND column_name = 'id'
        )
        """,
        table,
    )
    seq = f"{table}_id_seq"
    if not has_id:
        await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN id BIGINT")
        await conn.execute(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')")
        await conn.execute(f"UPDATE {table} SET id = nextval('{seq}') WHERE id IS NULL")
    else:
        await conn.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq}")
        await conn.execute(f"ALTER TABLE {table} ALTER COLUMN id SET DEFAULT nextval('{seq}')")
        await conn.execute(f"UPDATE {table} SET id = nextval('{seq}') WHERE id IS NULL")

    # Keep constraints idempotent for repeated deploy starts.
    await conn.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {table}_id_uidx ON {table}(id)")