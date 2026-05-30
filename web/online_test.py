"""Online test execution through residential proxy.

Active IP reuse: when city is random, prefer sessions from active_proxy_ips
(ping-gated — no ipify round-trip needed). Fall back to fresh sessions
if active pool is exhausted or all IPs are unpingable.
10 rounds (tcping) / 5 rounds (http) per IP, each round: 1 ping (tool→proxy) + 1 tcping (tool→target).
Supports city targeting via Oxylabs -city- parameter.
Random city: pick 5 random cities from proxy_cities table.
Tier 1 (DC proxy) removed — DC IP geolocation is inaccurate.
"""

from __future__ import annotations

import asyncio
import random
from typing import Any, Optional

from aiohttp import web

from config.settings import BaseConfig, get_base_config
from db import connection
from db.queries import get_cities_for_country, get_system_config, set_system_config
from proxy.provider import ProxyEntry, build_proxy_url, generate_session_id
from proxy_monitor.queries import get_random_active_ips, get_active_ips_for_country
from tester.tcp_ping import (
    ProxyAuth,
    ping_with_fallback,
    tcp_connect_rtt,
    get_proxy_ip,
    ping_proxy_ip,
    measure_gateway_rtt,
    _online_subprocess_executor,
)
from utils.logger import get_logger

logger = get_logger("online_test")

# Gateway RTT cache (in-memory, avoids ~9s ping per test)
_gw_rtt_cache: dict[str, float] = {}


async def _get_cached_gw_rtt(gateway: str, cache_key: str) -> float:
    """Get gateway RTT from system_config cache (24h TTL), or measure + cache."""
    # Check in-memory cache first
    if cache_key in _gw_rtt_cache:
        return _gw_rtt_cache[cache_key]

    # Check DB cache
    try:
        async with asyncio.timeout(5.0), connection.acquire_online() as conn:
            cached = await get_system_config(conn, cache_key)
        if cached is not None:
            _gw_rtt_cache[cache_key] = cached
            return cached
    except Exception:
        pass

    # Measure (2 packets, fast)
    rtt = await measure_gateway_rtt(gateway, count=2, timeout_sec=3.0, executor=_online_subprocess_executor)
    ms = rtt.median_ms if rtt else 0.0

    # Cache in DB and memory
    try:
        async with asyncio.timeout(5.0), connection.acquire_online() as conn:
            await set_system_config(conn, cache_key, ms)
    except Exception:
        pass
    _gw_rtt_cache[cache_key] = ms
    return ms


# Test parameters
SESSIONS_PER_COUNTRY = 5
ROUNDS_TCPING = 10
ROUNDS_HTTP = 5
ROUND_INTERVAL_SEC = 1.0
CONNECT_TIMEOUT = 10.0


def _parse_target(target: str) -> tuple[str, int]:
    """Parse target string into (hostname, port)."""
    if ":" in target:
        parts = target.rsplit(":", 1)
        try:
            return parts[0], int(parts[1])
        except ValueError:
            pass
    return target, 443


async def _resolve_cities(
    country_code: str, city_param: Optional[str],
) -> list[Optional[str]]:
    """Resolve city parameter into a list of cities (one per session).

    If city_param is None/empty/"random": pick 5 random cities from proxy_cities.
    If city_param is a specific city: use it for all 5 sessions.
    """
    if city_param and city_param.lower() not in ("random", ""):
        # Specific city — use for all sessions
        return [city_param] * SESSIONS_PER_COUNTRY

    # Random: pick from DB (proxy_cities uses lowercase country_code)
    try:
        async with asyncio.timeout(5.0), connection.acquire_online() as conn:
            all_cities = await get_cities_for_country(conn, country_code)
        if all_cities:
            # Pick 5 (with replacement so some may repeat)
            # all_cities is list of dicts {city, city_cn}; extract city name
            city_names = [c["city"] if isinstance(c, dict) else c for c in all_cities]
            result = [random.choice(city_names) for _ in range(SESSIONS_PER_COUNTRY)]
            logger.info("resolve_cities_random", country=country_code, cities=result)
            return result
    except Exception as e:
        logger.warning("resolve_cities_error", country=country_code, error=str(e))

    # Fallback: no city targeting
    return [None] * SESSIONS_PER_COUNTRY


async def _run_one_session(
    ws: web.WebSocketResponse,
    entry: ProxyEntry,
    config: BaseConfig,
    target: str,
    gateway_ip: str,
    cancel_event: asyncio.Event,
    session_num: int,
    city: Optional[str],
    protocol: str,
    active_session_id: Optional[str] = None,
    active_proxy_ip: Optional[str] = None,
    tool_gw_ms: float = 0.0,
) -> None:
    """Run one session: create proxy, get IP, protocol-dependent rounds (tcping=10, http=5).

    For HTTP: no ping, no calibration — raw TTFB is the result.
    For TCPing: each round does 1 ping + 1 tcping, calibrated = tcping - ping.

    IP source priority (per session):
    - active_session_id + active_proxy_ip: reuse active session (skip ipify)
    - Otherwise: fresh session (ipify + ping calibration)
    """
    hostname, port = _parse_target(target)
    actual_city = city
    proxy_type = "residential"

    MAX_PING_RETRIES = 3
    proxy_ip: Optional[str] = None
    session_id: Optional[str] = None
    proxy_auth: Optional[ProxyAuth] = None
    # Default: residential gateway
    proxy_host = gateway_ip
    proxy_port = config.gateway_port
    socks5_host = gateway_ip
    socks5_port = config.socks5_port

    # ── Tier 1: Active session (ping-only, no ipify) ──────
    if protocol == "tcping":
        # ── Tier 1: Active session (ping-only, no ipify) ──────
        if active_session_id and active_proxy_ip:
            ping_result = await ping_proxy_ip(active_proxy_ip, count=1, timeout_sec=5.0, executor=_online_subprocess_executor)
            if ping_result and ping_result.median_ms is not None:
                proxy_ip = active_proxy_ip
                session_id = active_session_id
                _, proxy_host, proxy_port, auth_user, auth_password = build_proxy_url(
                    entry, session_id, config, gateway_ip, sticky_ip=True, city=city,
                )
                proxy_auth = ProxyAuth(username=auth_user, password=auth_password)
                logger.info("session_active_ip_pingable", country=entry.iso2, session=session_num, proxy_ip=proxy_ip)
            else:
                logger.warning("session_active_ip_not_pingable", country=entry.iso2, session=session_num, proxy_ip=active_proxy_ip)

        # ── Tier 2: Fresh session (ipify + ping) ──────────────
        if not proxy_ip:
            for attempt in range(1, MAX_PING_RETRIES + 1):
                session_id = generate_session_id(entry.iso2)
                _, proxy_host, proxy_port, auth_user, auth_password = build_proxy_url(
                    entry, session_id, config, gateway_ip, sticky_ip=True, city=city,
                )
                proxy_auth = ProxyAuth(username=auth_user, password=auth_password)

                try:
                    proxy_ip = await get_proxy_ip(proxy_host, proxy_port, proxy_auth, executor=_online_subprocess_executor)
                except Exception as e:
                    logger.warning("session_get_ip_error", country=entry.iso2, session=session_num, attempt=attempt, error=str(e) or repr(e), error_type=type(e).__name__)

                if not proxy_ip and city:
                    # City not supported by proxy provider (e.g. 400) — drop session
                    logger.warning("session_city_unsupported", country=entry.iso2, session=session_num, city=city, attempt=attempt)
                    proxy_ip = None
                    break

                if not proxy_ip:
                    logger.warning("session_no_ip", country=entry.iso2, session=session_num, attempt=attempt)
                    proxy_ip = None
                    await asyncio.sleep(0.5)
                    continue

                # Verify proxy IP is pingable
                ping_result = await ping_proxy_ip(proxy_ip, count=1, timeout_sec=5.0, executor=_online_subprocess_executor)
                if ping_result and ping_result.median_ms is not None:
                    logger.info("session_ip_pingable", country=entry.iso2, session=session_num, proxy_ip=proxy_ip, attempt=attempt)
                    break
                logger.warning("session_ip_not_pingable", country=entry.iso2, session=session_num, proxy_ip=proxy_ip, attempt=attempt)
                proxy_ip = None
            else:
                # All retries exhausted — skip session
                logger.warning("session_no_pingable_ip", country=entry.iso2, session=session_num)
                await ws.send_json({
                    "type": "session_done",
                    "country": entry.iso2,
                    "country_en": entry.entry_point_en,
                    "country_cn": entry.entry_point_cn,
                    "city": actual_city or "",
                    "session": session_num,
                    "skipped": True,
                })
                return
    else:
        # HTTP: just get proxy IP, no ping verification
        if active_session_id and active_proxy_ip:
            # Reuse active session directly, no ipify needed
            session_id = active_session_id
            proxy_ip = active_proxy_ip
            _, proxy_host, proxy_port, auth_user, auth_password = build_proxy_url(
                entry, session_id, config, gateway_ip, sticky_ip=True, city=city,
            )
            proxy_auth = ProxyAuth(username=auth_user, password=auth_password)
        else:
            session_id = generate_session_id(entry.iso2)
            _, proxy_host, proxy_port, auth_user, auth_password = build_proxy_url(
                entry, session_id, config, gateway_ip, sticky_ip=True, city=city,
            )
            proxy_auth = ProxyAuth(username=auth_user, password=auth_password)
            try:
                proxy_ip = await get_proxy_ip(proxy_host, proxy_port, proxy_auth, executor=_online_subprocess_executor)
            except Exception as e:
                logger.warning("session_get_ip_error", country=entry.iso2, session=session_num, error=str(e) or repr(e), error_type=type(e).__name__)
            if not proxy_ip:
                logger.warning("session_no_ip", country=entry.iso2, session=session_num)

    # Warm-up packet for TCPing (discard first result to eliminate DNS resolution overhead)
    if protocol == "tcping":
        try:
            await ping_with_fallback(
                proxy_host, proxy_port, proxy_auth,
                hostname, port, timeout_sec=CONNECT_TIMEOUT,
                socks5_host=socks5_host, socks5_port=socks5_port,
            )
        except Exception:
            pass

    # Ping proxy IP once for calibration (reuse across all rounds)
    # proxy_rtt = tool_gw_ms + ping(proxy_ip) — two-leg model
    cached_proxy_rtt_ms: Optional[float] = None
    if protocol == "tcping" and proxy_ip:
        ping_result = await ping_proxy_ip(proxy_ip, count=1, timeout_sec=5.0, executor=_online_subprocess_executor)
        if ping_result and ping_result.median_ms is not None:
            cached_proxy_rtt_ms = tool_gw_ms + ping_result.median_ms

    # Run rounds serially (real-time streaming), using cached proxy_rtt
    rounds = ROUNDS_TCPING if protocol == "tcping" else ROUNDS_HTTP
    all_calibrated_rtts: list[float] = []
    for round_idx in range(1, rounds + 1):
        if cancel_event.is_set():
            return

        proxy_rtt_ms = cached_proxy_rtt_ms
        target_rtt_ms: Optional[float] = None
        calibrated_rtt_ms: Optional[float] = None
        error: Optional[str] = None
        status_code: Optional[int] = None

        try:
            # TCPing/HTTP to target
            if protocol == "tcping":
                result = await ping_with_fallback(
                    proxy_host, proxy_port, proxy_auth,
                    hostname, port, timeout_sec=CONNECT_TIMEOUT,
                    socks5_host=socks5_host, socks5_port=socks5_port,
                )
                target_rtt_ms = result.rtt_ms
                if not result.rtt_ms:
                    error = result.error or "timeout"
            else:
                # HTTP CONNECT — raw TTFB, no calibration
                result = await tcp_connect_rtt(
                    proxy_host, proxy_port, proxy_auth,
                    hostname, port, CONNECT_TIMEOUT,
                )
                target_rtt_ms = result.rtt_ms
                status_code = result.status_code
                if not result.rtt_ms:
                    error = result.error or "timeout"

            # Calibrate (TCPing only)
            if protocol == "tcping" and target_rtt_ms is not None and proxy_rtt_ms is not None:
                calibrated_rtt_ms = max(target_rtt_ms - proxy_rtt_ms, 0.0)

            # Collect calibrated RTT for median computation
            if calibrated_rtt_ms is not None:
                all_calibrated_rtts.append(calibrated_rtt_ms)

        except Exception as e:
            error = str(e)

        # Stream result to frontend immediately
        msg: dict[str, Any] = {
            "type": "probe",
            "country": entry.iso2,
            "country_en": entry.entry_point_en,
            "country_cn": entry.entry_point_cn,
            "city": actual_city or "",
            "session": session_num,
            "round": round_idx,
            "proxy_rtt_ms": proxy_rtt_ms,
            "target_rtt_ms": target_rtt_ms,
            "calibrated_rtt_ms": calibrated_rtt_ms,
            "proxy_ip": proxy_ip,
            "proxy_type": proxy_type,
            "error": error,
        }
        if round_idx == 1:
            logger.info("probe_first", country=entry.iso2, city=actual_city, session=session_num, proxy_ip=proxy_ip, proxy_type=proxy_type)
        if protocol == "http":
            msg["status_code"] = status_code
        await ws.send_json(msg)

        # Interval between rounds (skip after last round)
        if round_idx < rounds and ROUND_INTERVAL_SEC > 0:
            await asyncio.sleep(ROUND_INTERVAL_SEC)

    # Session done — compute median from all calibrated RTTs
    median_rtt_ms: Optional[float] = None
    if all_calibrated_rtts:
        sorted_rtts = sorted(all_calibrated_rtts)
        n = len(sorted_rtts)
        mid = n // 2
        median_rtt_ms = round(sorted_rtts[mid] if n % 2 else (sorted_rtts[mid - 1] + sorted_rtts[mid]) / 2, 3)

    await ws.send_json({
        "type": "session_done",
        "country": entry.iso2,
        "country_en": entry.entry_point_en,
        "country_cn": entry.entry_point_cn,
        "city": actual_city or "",
        "session": session_num,
        "median_rtt_ms": median_rtt_ms,
    })


async def run_country_test(
    ws: web.WebSocketResponse,
    entry: ProxyEntry,
    config: BaseConfig,
    target: str,
    gateway_ip: str,
    cancel_event: asyncio.Event,
    cities: list[Optional[str]],
    protocol: str,
    sessions: int = SESSIONS_PER_COUNTRY,
    active_sessions: Optional[list[tuple[str, str, Optional[str]]]] = None,
    tool_gw_ms: float = 0.0,
) -> None:
    """Run test for one country: `sessions` sessions in parallel, protocol-dependent rounds each.

    IP source priority per session:
    - active_sessions: list of (session_id, proxy_ip, city) from active_proxy_ips (Tier 1)
    - Fresh sessions fill remaining slots (Tier 2)
    """
    active_count = min(len(active_sessions or []), sessions)
    fresh_count = sessions - active_count

    if sessions > 0:
        logger.info(
            "session_tier_allocation",
            country=entry.iso2,
            total=sessions,
            active=active_count,
            fresh=fresh_count,
        )

    tasks = []
    for session_num in range(1, sessions + 1):
        city = cities[session_num - 1] if session_num <= len(cities) else None

        # Tier 1: Active session
        act_sid, act_ip, act_city = (None, None, None)
        if session_num <= active_count and active_sessions:
            act_sid, act_ip, act_city = active_sessions[session_num - 1]
        # Use active IP's city as fallback, but prefer user-selected city
        if act_sid and not city:
            city = act_city

        # Tier 2: Fresh session (no active_session_id)
        tasks.append(
            _run_one_session_safe(
                ws, entry, config, target, gateway_ip,
                cancel_event, session_num, city, protocol,
                active_session_id=act_sid, active_proxy_ip=act_ip,
                tool_gw_ms=tool_gw_ms,
            )
        )
    await asyncio.gather(*tasks, return_exceptions=True)


async def _run_one_session_safe(
    ws: web.WebSocketResponse,
    entry: ProxyEntry,
    config: BaseConfig,
    target: str,
    gateway_ip: str,
    cancel_event: asyncio.Event,
    session_num: int,
    city: Optional[str],
    protocol: str,
    active_session_id: Optional[str] = None,
    active_proxy_ip: Optional[str] = None,
    tool_gw_ms: float = 0.0,
) -> None:
    """Wrapper that catches session-level exceptions and sends error probes."""
    try:
        await _run_one_session(
            ws, entry, config, target, gateway_ip,
            cancel_event, session_num, city, protocol,
            active_session_id=active_session_id, active_proxy_ip=active_proxy_ip,
            tool_gw_ms=tool_gw_ms,
        )
    except Exception as e:
        logger.warning(
            "session_error",
            country=entry.iso2, session=session_num, error=str(e),
        )
        # Send error probes for all rounds
        rounds = ROUNDS_TCPING if protocol == "tcping" else ROUNDS_HTTP
        for round_idx in range(1, rounds + 1):
            await ws.send_json({
                "type": "probe",
                "country": entry.iso2,
                "country_en": entry.entry_point_en,
                "country_cn": entry.entry_point_cn,
                "city": city or "",
                "session": session_num,
                "round": round_idx,
                "proxy_rtt_ms": None,
                "target_rtt_ms": None,
                "calibrated_rtt_ms": None,
                "proxy_ip": None,
                "error": str(e),
            })
        await ws.send_json({
            "type": "session_done",
            "country": entry.iso2,
            "country_en": entry.entry_point_en,
            "country_cn": entry.entry_point_cn,
            "city": city or "",
            "session": session_num,
        })


async def run_global_test(
    ws: web.WebSocketResponse,
    config: BaseConfig,
    target: str,
    gateway_ip: str,
    cancel_event: asyncio.Event,
    protocol: str,
    tool_gw_ms: float = 0.0,
) -> None:
    """Run test in global mode: pick random countries, 1 session each."""
    entries: list[ProxyEntry] = []
    try:
        async with asyncio.timeout(5.0), connection.acquire_online() as conn:
            rows = await conn.fetch(
                """
                SELECT id, entry_point_en, entry_point_cn, iso2, class
                FROM proxy_entry_points
                WHERE class = 'A'
                ORDER BY RANDOM()
                LIMIT $1
                """,
                SESSIONS_PER_COUNTRY,
            )
            for r in rows:
                entries.append(ProxyEntry(
                    id=r["id"],
                    entry_point_en=r["entry_point_en"],
                    entry_point_cn=r["entry_point_cn"],
                    class_level=r["class"],
                    iso2=r["iso2"],
                ))
    except Exception as e:
        logger.warning("global_country_select_error", error=str(e))

    if not entries:
        entries = [ProxyEntry(
            id=0, entry_point_en="Global", entry_point_cn="全球",
            class_level="A", iso2="global",
        )]

    tasks = []
    for entry in entries:
        cities = await _resolve_cities(entry.iso2, None)

        # Tier 1: Active session
        active_sessions = None
        try:
            async with asyncio.timeout(5.0), connection.acquire_online() as conn:
                active_ips = await get_random_active_ips(conn, entry.iso2, 1)
            if active_ips:
                active_sessions = [(a.session_id, a.proxy_ip, a.city) for a in active_ips]
                logger.info("global_active_session_reuse", country=entry.iso2)
        except Exception as e:
            logger.warning("global_active_session_fetch_error", country=entry.iso2, error=str(e))

        tasks.append(
            run_country_test(
                ws, entry, config, target, gateway_ip,
                cancel_event, cities=cities, protocol=protocol, sessions=1,
                active_sessions=active_sessions,
                tool_gw_ms=tool_gw_ms,
            )
        )
    await asyncio.gather(*tasks, return_exceptions=True)


async def handle_online_test(
    ws: web.WebSocketResponse,
    data: dict[str, Any],
    cancel_event: asyncio.Event,
) -> None:
    """Handle online test WebSocket request.

    Expected data:
      type: "start"
      protocol: "tcping" | "http"
      target: target string (hostname or hostname:port)
      country: ISO2 code (empty/absent for global mode)
      global: bool - if true, use random country selection
      city: string - city name for targeting, or "random"/empty for random city
    """
    protocol = data.get("protocol", "tcping")
    target = data.get("target", "")
    country_iso2 = data.get("country", "") or (data.get("countries", [""])[0] if data.get("countries") else "")
    is_global = data.get("global", False) or not country_iso2
    city_param = data.get("city", "")

    # Support legacy targets list
    if not target and data.get("targets"):
        target = data["targets"][0]

    if not target:
        await ws.send_json({"type": "error", "message": "No target specified"})
        await ws.send_json({"type": "done"})
        return

    config = get_base_config()
    from proxy.provider import resolve_gateway_ip
    gateway_ip = resolve_gateway_ip(config.gateway_ips) if config.gateway_ips else "127.0.0.1"

    # Get residential gateway RTT from cache (avoids ~9s ping per test)
    tool_gw_ms_val = 0.0
    if gateway_ip:
        tool_gw_ms_val = await _get_cached_gw_rtt(gateway_ip, "tool_gw_ms")

    await ws.send_json({"type": "target_start", "target": target})

    if is_global:
        # Global mode: random country selection
        try:
            await run_global_test(ws, config, target, gateway_ip, cancel_event, protocol, tool_gw_ms=tool_gw_ms_val)
        except Exception as e:
            logger.error("global_test_error", error=str(e))
            await ws.send_json({"type": "error", "message": str(e)})
    else:
        # Single country mode
        try:
            async with asyncio.timeout(5.0), connection.acquire_online() as conn:
                row = await conn.fetchrow(
                    "SELECT id, entry_point_en, entry_point_cn, iso2, class "
                    "FROM proxy_entry_points WHERE iso2 = $1",
                    country_iso2,
                )
        except Exception as e:
            logger.error("fetch_proxy_entry_error", error=str(e))
            await ws.send_json({"type": "error", "message": f"DB error: {e}"})
            await ws.send_json({"type": "done"})
            return

        if not row:
            await ws.send_json({"type": "error", "message": f"Country not found: {country_iso2}"})
            await ws.send_json({"type": "done"})
            return

        entry = ProxyEntry(
            id=row["id"],
            entry_point_en=row["entry_point_en"],
            entry_point_cn=row["entry_point_cn"],
            class_level=row["class"],
            iso2=row["iso2"],
        )

        cities = await _resolve_cities(entry.iso2, city_param if city_param and city_param.lower() not in ("random", "") else None)

        is_random_city = not city_param or city_param.lower() in ("random", "")

        # Tier 1: Active residential IPs — only when city is Global/Random
        # (specific city mode uses fresh sessions with -city- parameter for accuracy)
        active_sessions: Optional[list[tuple[str, str, Optional[str]]]] = None
        active_count_needed = SESSIONS_PER_COUNTRY if is_random_city else 0
        if active_count_needed > 0:
            try:
                async with asyncio.timeout(5.0), connection.acquire_online() as conn:
                    active_ips = await get_active_ips_for_country(conn, entry.iso2, active_count_needed)
                if active_ips:
                    active_sessions = [(a.session_id, a.proxy_ip, a.city) for a in active_ips]
                    logger.info("active_sessions_reuse", country=entry.iso2, count=len(active_sessions))
            except Exception as e:
                logger.warning("active_sessions_fetch_error", country=entry.iso2, error=str(e))

        try:
            await run_country_test(
                ws, entry, config, target, gateway_ip,
                cancel_event, cities=cities, protocol=protocol,
                active_sessions=active_sessions,
                tool_gw_ms=tool_gw_ms_val,
            )
        except Exception as e:
            logger.error("online_test_error", target=target, error=str(e))
            await ws.send_json({"type": "error", "message": str(e)})

        await ws.send_json({
            "type": "country_done",
            "country": entry.iso2,
            "country_en": entry.entry_point_en,
            "country_cn": entry.entry_point_cn,
        })

    await ws.send_json({"type": "target_done", "target": target})
    await ws.send_json({"type": "done"})
