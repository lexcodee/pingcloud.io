"""aiohttp web application creation and route setup."""

from __future__ import annotations

from pathlib import Path

from aiohttp import web

from web.api import setup_api_routes
from utils.logger import get_logger

logger = get_logger("web")

_STATIC_DIR = Path(__file__).parent / "static"


@web.middleware
async def _static_data_cache_middleware(request: web.Request, handler):
    """Set long-lived Cache-Control for /static/data/ JSON files (CDN-friendly)."""
    resp = await handler(request)
    if request.path.startswith("/static/data/"):
        resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


async def _on_startup(app: web.Application) -> None:
    """Initialize DB pools on app startup if not already initialized."""
    from db import connection
    from config.settings import get_base_config
    config = get_base_config()
    if not connection._pool:
        await connection.init_pool(config)
        logger.info("db_pool_initialized_on_startup")
    if not connection._online_pool:
        await connection.init_online_pool(config)
        logger.info("online_db_pool_initialized_on_startup")


async def _on_cleanup(app: web.Application) -> None:
    """Close DB pools on app cleanup."""
    from db import connection
    if connection._online_pool:
        await connection.close_online_pool()
        logger.info("online_db_pool_closed_on_cleanup")
    if connection._pool:
        await connection.close_pool()
        logger.info("db_pool_closed_on_cleanup")


def create_web_app() -> web.Application:
    """Create the aiohttp web application with all routes."""
    app = web.Application(middlewares=[_static_data_cache_middleware])

    # Lifecycle hooks
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    # Static files
    app.router.add_static("/static", _STATIC_DIR, name="static")

    # Index page
    async def index_handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "index.html")

    app.router.add_get("/", index_handler)
    # Sitemap page
    async def sitemap_handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "sitemap.xml")
#    app.router.add_get("/sitemap.xml", sitemap_handler)

    # Baidu site verification (must be at root path)
    async def baidu_verify_handler(request: web.Request) -> web.FileResponse:
        return web.FileResponse(_STATIC_DIR / "baidu_verify_codeva-1eHVwxgjsf.html")

    app.router.add_get("/baidu_verify_codeva-1eHVwxgjsf.html", baidu_verify_handler)

    # API routes
    setup_api_routes(app)

    logger.info("web_app_created")
    return app
