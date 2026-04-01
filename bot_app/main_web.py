"""Web process entrypoint helpers."""

from __future__ import annotations

from fastapi import FastAPI

from .config import load_role_mapping, load_settings
from .db import init_db
from .logging_utils import setup_logging
from .osu_client import OsuClient
from .web_app import create_web_app


async def build_web_app():
    """Build FastAPI app with configured dependencies."""
    setup_logging()
    settings = load_settings()
    await init_db(settings.database_path)
    osu_client = OsuClient(
        settings.osu_client_id, settings.osu_client_secret, cache_ttl=settings.osu_cache_ttl_seconds
    )
    role_mapping = load_role_mapping(settings.role_mapping_path)
    app = create_web_app(settings=settings, osu_client=osu_client, role_mapping=role_mapping)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await osu_client.close()

    return app


def create_app() -> FastAPI:
    """Create FastAPI app for ASGI servers (uvicorn/gunicorn)."""
    setup_logging()
    settings = load_settings()
    osu_client = OsuClient(
        settings.osu_client_id, settings.osu_client_secret, cache_ttl=settings.osu_cache_ttl_seconds
    )
    role_mapping = load_role_mapping(settings.role_mapping_path)
    app = create_web_app(settings=settings, osu_client=osu_client, role_mapping=role_mapping)

    @app.on_event("startup")
    async def _startup() -> None:
        await init_db(settings.database_path)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await osu_client.close()

    return app
