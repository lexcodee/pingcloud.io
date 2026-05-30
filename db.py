"""Database connection pool and CRUD operations using asyncpg."""

import asyncpg
import logging
from datetime import datetime, timezone

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME,
            min_size=2, max_size=20,
        )
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ── Schema ──────────────────────────────────────────────────────────────

async def ensure_tables():
    """Create latency_results and hourly_quota if they don't exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS latency_results (
                id               SERIAL PRIMARY KEY,
                city_id          INT REFERENCES cities(id),
                endpoint_id      INT REFERENCES cloud_endpoints(id),
                measurement_id   VARCHAR(64),
                avg_ms           FLOAT,
                median_ms        FLOAT,
                loss_pct         FLOAT,
                probe_ip         inet,
                mdev_ms          FLOAT,
                tested_at        TIMESTAMPTZ DEFAULT NOW(),
                test_round       VARCHAR(32)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS hourly_quota (
                hour_bucket  TIMESTAMPTZ PRIMARY KEY,
                test_count   INT DEFAULT 0
            );
        """)
        # Helpful indexes
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lr_city_ep
            ON latency_results (city_id, endpoint_id);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lr_tested_at
            ON latency_results (tested_at);
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_lr_test_round
            ON latency_results (test_round);
        """)
        # Widen test_round column for timestamped refresh rounds (e.g. "rh_20260531_0450")
        await conn.execute("""
            ALTER TABLE latency_results
            ALTER COLUMN test_round TYPE VARCHAR(32)
        """)
        # Drop unique constraint — each refresh cycle now inserts new rows
        await conn.execute("""
            ALTER TABLE latency_results
            DROP CONSTRAINT IF EXISTS uq_city_ep_round
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_state (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
        """)
    logger.info("Database tables ensured")


# ── Read helpers ────────────────────────────────────────────────────────

async def fetch_all_cities() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM cities ORDER BY id")
    return [dict(r) for r in rows]


async def fetch_all_endpoints() -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM cloud_endpoints ORDER BY id")
    return [dict(r) for r in rows]


async def fetch_existing_pairs(test_round: str | None = None) -> set[tuple[int, int]]:
    """Return set of (city_id, endpoint_id) already successfully tested.

    A pair is considered done if it has at least one result with loss_pct < 100
    (i.e. at least some packets got through). Pairs with only 100% loss results
    are not included so they will be re-tested.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if test_round:
            rows = await conn.fetch(
                "SELECT city_id, endpoint_id FROM latency_results"
                " WHERE test_round = $1 AND loss_pct < 100",
                test_round,
            )
        else:
            rows = await conn.fetch(
                "SELECT city_id, endpoint_id FROM latency_results WHERE loss_pct < 100"
            )
    return {(r["city_id"], r["endpoint_id"]) for r in rows}


async def fetch_latest_results() -> list[dict]:
    """Fetch the latest latency_result per (city_id, endpoint_id) pair."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (city_id, endpoint_id)
                   lr.*, c.country_iso2, c.country_en, c.country_cn,
                   ce.vendor, ce.region_name
            FROM latency_results lr
            JOIN cities c ON c.id = lr.city_id
            JOIN cloud_endpoints ce ON ce.id = lr.endpoint_id
            WHERE lr.median_ms >= 1.0
            ORDER BY city_id, endpoint_id, tested_at DESC
        """)
    return [dict(r) for r in rows]


async def fetch_results_for_period(days: int) -> list[dict]:
    """Fetch aggregated latency results for the last N days.

    Returns one row per (city_id, endpoint_id) with averaged stats
    and a test_count of how many measurements fall in the window.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                lr.city_id, lr.endpoint_id,
                c.country_iso2, c.country_en, c.country_cn,
                ce.vendor, ce.region_name,
                AVG(lr.avg_ms)    AS avg_ms,
                AVG(lr.median_ms) AS median_ms,
                AVG(lr.loss_pct)  AS loss_pct,
                COUNT(*)          AS test_count
            FROM latency_results lr
            JOIN cities c ON c.id = lr.city_id
            JOIN cloud_endpoints ce ON ce.id = lr.endpoint_id
            WHERE lr.tested_at >= NOW() - ($1 || ' days')::INTERVAL
              AND lr.loss_pct < 100
              AND lr.median_ms >= 1.0
            GROUP BY lr.city_id, lr.endpoint_id,
                     c.country_iso2, c.country_en, c.country_cn,
                     ce.vendor, ce.region_name
        """, str(days))
    return [dict(r) for r in rows]


async def fetch_country_region_map() -> dict[str, str]:
    """Return {country_iso2: region} mapping from the cities table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT country_iso2, region
            FROM cities
            WHERE country_iso2 IS NOT NULL AND region IS NOT NULL
        """)
    return {r["country_iso2"]: r["region"] for r in rows}


# ── Write helpers ───────────────────────────────────────────────────────

async def insert_result(
    city_id: int,
    endpoint_id: int,
    measurement_id: str,
    avg_ms: float | None,
    median_ms: float | None,
    loss_pct: float | None,
    probe_ip: str | None,
    mdev_ms: float | None,
    test_round: str,
) -> int | None:
    """Insert a latency result. Returns row id."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rid = await conn.fetchval("""
                INSERT INTO latency_results
                    (city_id, endpoint_id, measurement_id,
                     avg_ms, median_ms, loss_pct, probe_ip, mdev_ms, test_round)
                VALUES ($1,$2,$3,$4,$5,$6,$7::inet,$8,$9)
                RETURNING id
            """, city_id, endpoint_id, measurement_id,
                 avg_ms, median_ms, loss_pct, probe_ip, mdev_ms, test_round)
    return rid


async def set_probe_num(city_id: int, probe_num: int):
    """Update the probe_num column for a city."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cities SET probe_num = $2 WHERE id = $1",
            city_id, probe_num,
        )


async def mark_city_probe_online(city_id: int):
    """Set probe_num = 1 for a city (probe found during testing)."""
    await set_probe_num(city_id, 1)


# ── Quota ───────────────────────────────────────────────────────────────

def _truncate_to_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0, tzinfo=timezone.utc)


async def check_and_consume_quota(hourly_limit: int) -> bool:
    """Atomically increment quota for current hour.
    Returns True if under limit (allowed), False if over limit."""
    pool = await get_pool()
    bucket = _truncate_to_hour(datetime.now(timezone.utc))
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT hourly_quota.test_count FROM hourly_quota WHERE hour_bucket = $1",
                bucket,
            )
            current = row["test_count"] if row else 0
            if current >= hourly_limit:
                return False
            await conn.execute("""
                INSERT INTO hourly_quota (hour_bucket, test_count)
                VALUES ($1, 1)
                ON CONFLICT (hour_bucket)
                DO UPDATE SET test_count = hourly_quota.test_count + 1
            """, bucket)
    return True


async def get_current_quota_count() -> int:
    pool = await get_pool()
    bucket = _truncate_to_hour(datetime.now(timezone.utc))
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT test_count FROM hourly_quota WHERE hour_bucket = $1",
            bucket,
        )
    return row["test_count"] if row else 0


async def force_exhaust_quota(hourly_limit: int):
    """Force-set current hour's quota count to the limit.

    Called when a 429 is received from the API, so that all concurrent
    tasks calling check_and_consume_quota() will also block until next hour.
    """
    pool = await get_pool()
    bucket = _truncate_to_hour(datetime.now(timezone.utc))
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO hourly_quota (hour_bucket, test_count)
            VALUES ($1, $2)
            ON CONFLICT (hour_bucket)
            DO UPDATE SET test_count = $2
        """, bucket, hourly_limit)


# ── Task state ──────────────────────────────────────────────────────────

async def get_task_state(key: str) -> str | None:
    """Read a value from task_state by key. Returns None if not found."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT value FROM task_state WHERE key = $1", key,
        )
    return row["value"] if row else None


async def set_task_state(key: str, value: str):
    """Upsert a key/value pair in task_state."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO task_state (key, value, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = $2, updated_at = NOW()
        """, key, value)
