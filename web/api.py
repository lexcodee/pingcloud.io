"""API route handlers — REST endpoints and WebSocket."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from aiohttp import web

from config.settings import get_base_config
from db import connection, queries
from utils.logger import get_logger

logger = get_logger("web_api")

# ── Online test daily rate limit (per client IP, in-memory) ──
_daily_test_counts: dict[str, tuple[date, int]] = {}


def _check_daily_limit(client_ip: str) -> tuple[bool, int, int]:
    """Check if client IP has exceeded daily test limit.

    Returns (allowed, current_count, limit).
    """
    config = get_base_config()
    limit = config.online_test_daily_limit
    if limit <= 0:
        return True, 0, limit

    today = date.today()
    entry = _daily_test_counts.get(client_ip)
    if entry and entry[0] == today:
        count = entry[1]
    else:
        count = 0

    if count >= limit:
        return False, count, limit

    # Increment
    _daily_test_counts[client_ip] = (today, count + 1)
    return True, count + 1, limit


async def handle_active_proxy_ips(request: web.Request) -> web.Response:
    """GET /api/monitor/active-ips — Return active proxy IPs per country.

    Query params:
      country: filter by ISO2 code (optional)
    """
    try:
        from proxy_monitor import queries as monitor_queries
        country = request.query.get("country")
        async with asyncio.timeout(3.0), connection.acquire_online() as conn:
            if country:
                rows = await monitor_queries.get_all_active_for_country(conn, country)
            else:
                rows = await monitor_queries.get_all_active_for_verification(conn)
        data = [
            {
                "country_code": r.country_code,
                "session_id": r.session_id,
                "proxy_ip": r.proxy_ip,
                "tool_to_proxy_rtt_ms": r.tool_to_proxy_rtt_ms,
                "tool_to_proxy_rtt_mdev_ms": r.tool_to_proxy_rtt_mdev_ms,
                "first_test_at": r.first_test_at.isoformat(),
                "last_test_at": r.last_test_at.isoformat(),
            }
            for r in rows
        ]
        return web.json_response(data)
    except Exception as e:
        logger.error("api_active_ips_error", error=str(e))
        return web.json_response({"error": str(e)}, status=500)


async def handle_cities(request: web.Request) -> web.Response:
    """GET /api/cities?country=XX — Return cities for a country."""
    country = request.query.get("country", "")
    if not country:
        return web.json_response([])
    try:
        async with asyncio.timeout(3.0), connection.acquire_online() as conn:
            cities = await queries.get_cities_for_country(conn, country)
        return web.json_response(cities)
    except Exception as e:
        logger.error("api_cities_error", error=str(e))
        return web.json_response({"error": str(e)}, status=500)


def setup_api_routes(app: web.Application) -> None:
    """Register all API routes on the app."""
    app.router.add_get("/api/countries", handle_countries)
    app.router.add_get("/api/endpoints", handle_endpoints)
    app.router.add_get("/api/ranking/region", handle_region_ranking)
    app.router.add_get("/api/ranking/country", handle_country_ranking)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/api/monitor/active-ips", handle_active_proxy_ips)
    app.router.add_get("/api/cities", handle_cities)
    app.router.add_get("/ws/test", handle_test_ws)


async def handle_countries(request: web.Request) -> web.Response:
    """GET /api/countries — Return all proxy entry points."""
    try:
        async with asyncio.timeout(3.0), connection.acquire_online() as conn:
            rows = await conn.fetch(
                "SELECT id, entry_point_en, entry_point_cn, iso2, class "
                "FROM proxy_entry_points ORDER BY entry_point_en"
            )
        data = [
            {
                "id": r["id"],
                "en": r["entry_point_en"],
                "cn": r["entry_point_cn"],
                "iso2": r["iso2"],
                "class": r["class"],
            }
            for r in rows
        ]
        return web.json_response(data)
    except Exception as e:
        logger.error("api_countries_error", error=str(e))
        return web.json_response({"error": str(e)}, status=500)


async def handle_endpoints(request: web.Request) -> web.Response:
    """GET /api/endpoints — Return all cloud endpoints."""
    try:
        async with asyncio.timeout(3.0), connection.acquire_online() as conn:
            rows = await conn.fetch(
                "SELECT id, vendor, region_name, region_id, endpoint "
                "FROM cloud_endpoints ORDER BY vendor, region_id"
            )
        data = [
            {
                "id": r["id"],
                "vendor": r["vendor"],
                "region_name": r["region_name"],
                "region_id": r["region_id"],
                "endpoint": r["endpoint"],
            }
            for r in rows
        ]
        return web.json_response(data)
    except Exception as e:
        logger.error("api_endpoints_error", error=str(e))
        return web.json_response({"error": str(e)}, status=500)


def _load_ranking_json(filename: str) -> dict | None:
    """Load ranking JSON from output directory."""
    config = get_base_config()
    output_dir = Path(config.ranking_json_output_dir)
    filepath = output_dir / filename
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("ranking_json_load_error", path=str(filepath), error=str(e))
        return None


async def handle_region_ranking(request: web.Request) -> web.Response:
    """GET /api/ranking/region — Region ranking data.

    Query params:
      country: filter by country name (optional)
      days: filter by ranking_days (optional)
    """
    data = _load_ranking_json("region_ranking_latest.json")
    if data is None:
        return web.json_response({"periods": []})

    country_filter = request.query.get("country")
    days_filter = request.query.get("days")

    if days_filter:
        try:
            days_val = int(days_filter)
            data["periods"] = [p for p in data["periods"] if p["ranking_days"] == days_val]
        except ValueError:
            pass

    if country_filter:
        for period in data["periods"]:
            period["data"] = [
                d for d in period["data"]
                if country_filter.lower() in d.get("country_en", "").lower()
                or country_filter in d.get("country_cn", "")
            ]

    return web.json_response(data)


async def handle_country_ranking(request: web.Request) -> web.Response:
    """GET /api/ranking/country — Country ranking data.

    Query params:
      vendor: filter by vendor (optional)
      region: filter by region_id (optional)
      days: filter by ranking_days (optional)
    """
    data = _load_ranking_json("country_ranking_latest.json")
    if data is None:
        return web.json_response({"periods": []})

    vendor_filter = request.query.get("vendor")
    region_filter = request.query.get("region")
    days_filter = request.query.get("days")

    if days_filter:
        try:
            days_val = int(days_filter)
            data["periods"] = [p for p in data["periods"] if p["ranking_days"] == days_val]
        except ValueError:
            pass

    if vendor_filter:
        for period in data["periods"]:
            period["data"] = [
                d for d in period["data"]
                if d.get("vendor", "").lower() == vendor_filter.lower()
            ]

    if region_filter:
        for period in data["periods"]:
            period["data"] = [
                d for d in period["data"]
                if region_filter in d.get("region_id", "")
            ]

    return web.json_response(data)


async def handle_config(request: web.Request) -> web.Response:
    """GET /api/config — Return frontend-relevant config."""
    config = get_base_config()
    return web.json_response({
        "ranking_region_days": config.ranking_region_days,
        "ranking_country_days": config.ranking_country_days,
        "proxy_provider": config.proxy_provider,
    })


async def handle_test_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket /ws/test — Online test handler."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    cancel_event = asyncio.Event()
    test_task = None

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if data.get("type") == "start":
                    # Daily rate limit check
                    client_ip = (request.headers.get('CF-Connecting-IP')
                                 or request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
                                 or request.remote
                                 or "unknown")
                    allowed, count, limit = _check_daily_limit(client_ip)
                    if not allowed:
                        await ws.send_json({
                            "type": "error",
                            "message": f"Daily test limit reached ({count}/{limit})",
                            "limit_exceeded": True,
                        })
                        await ws.send_json({"type": "done"})
                        continue
                    # Log user test access (fire-and-forget, never block test)
                    try:
                        async with asyncio.timeout(2):
                            async with connection.acquire_online() as conn:
                                await queries.insert_user_test_log(
                                    conn,
                                    user_ip=client_ip,
                                    source_country=data.get("country", "") or "global",
                                    target=data.get("target", "") or "",
                                    protocol=data.get("protocol", "") or "tcping",
                                )
                    except Exception as e:
                        logger.warning("user_test_log_error", error=str(e))

                    from web.online_test import handle_online_test
                    from utils.priority import online_test_enter, online_test_exit
                    await online_test_enter()
                    cancel_event.clear()
                    test_task = asyncio.create_task(
                        handle_online_test(ws, data, cancel_event)
                    )
                elif data.get("type") == "cancel":
                    cancel_event.set()
                    await ws.send_json({"type": "cancelled"})
            elif msg.type == web.WSMsgType.ERROR:
                logger.error("ws_error", error=ws.exception())
    except Exception as e:
        logger.error("ws_handler_error", error=str(e))
    finally:
        from utils.priority import online_test_exit
        await online_test_exit()
        if test_task and not test_task.done():
            test_task.cancel()
            try:
                await test_task
            except asyncio.CancelledError:
                pass
        await ws.close()

    return ws
