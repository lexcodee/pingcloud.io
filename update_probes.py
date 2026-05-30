"""Fetch live probe list from Globalping API and update cities.probe_num."""

import asyncio
import json
import logging
import aiohttp
import asyncpg
from collections import Counter

from config import DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME

PROBES_URL = "https://api.globalping.io/v1/probes"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def fetch_probes(session: aiohttp.ClientSession) -> list[dict]:
    async with session.get(PROBES_URL) as resp:
        resp.raise_for_status()
        return await resp.json()


async def update_probes(
    session: aiohttp.ClientSession,
    pool: asyncpg.Pool,
) -> int:
    """Fetch live probes from Globalping and update cities.probe_num in DB.

    Accepts an existing aiohttp session and asyncpg pool (reuses them).
    Returns the number of matched cities updated.
    """
    logger.info("Fetching probe list from %s", PROBES_URL)
    probes = await fetch_probes(session)
    logger.info("Total probes: %d", len(probes))

    # Build (country_iso2, city) -> probe_count and preserve location metadata
    probe_counts: Counter[tuple[str, str]] = Counter()
    probe_locations: dict[tuple[str, str], dict] = {}
    for p in probes:
        loc = p["location"]
        key = (loc["country"], loc["city"])
        probe_counts[key] += 1
        if key not in probe_locations:
            probe_locations[key] = loc

    logger.info("Unique (country, city) pairs in probes: %d", len(probe_counts))

    async with pool.acquire() as conn:
        # Reset all probe_num to 0 first
        await conn.execute("UPDATE cities SET probe_num = 0")
        logger.info("Reset all cities.probe_num to 0")

        # Build update list: match DB cities against probe_counts
        cities = await conn.fetch("SELECT id, country_iso2, city FROM cities")

        matched = 0
        unmatched_probes = set(probe_counts.keys())
        updates: list[tuple[int, int]] = []

        for row in cities:
            key = (row["country_iso2"].strip(), row["city"])
            if key in probe_counts:
                updates.append((row["id"], probe_counts[key]))
                matched += 1
                unmatched_probes.discard(key)

        # Batch update using unnest
        if updates:
            ids = [u[0] for u in updates]
            nums = [u[1] for u in updates]
            await conn.execute(
                "UPDATE cities SET probe_num = data.num FROM (SELECT unnest($1::int[]) AS id, unnest($2::int[]) AS num) AS data WHERE cities.id = data.id",
                ids, nums,
            )
            logger.info("Updated probe_num for %d cities", matched)

        logger.info("Matched: %d / %d DB cities", matched, len(cities))

        # Insert probe cities not yet in DB, reusing country metadata from existing rows
        if unmatched_probes:
            logger.info("Probe cities not in DB (%d), inserting...", len(unmatched_probes))

            # Fetch country metadata from existing rows (one per country)
            country_meta = {}
            needed_countries = {c for c, _ in unmatched_probes}
            for row in await conn.fetch(
                "SELECT DISTINCT ON (country_iso2) country_iso2, country_en, country_cn, continent, region FROM cities WHERE country_iso2 = ANY($1) ORDER BY country_iso2, id",
                list(needed_countries),
            ):
                country_meta[row["country_iso2"]] = row

            # ISO2 -> (en, cn) for countries not yet in DB
            COUNTRY_NAMES: dict[str, tuple[str, str]] = {
                "UG": ("Uganda", "乌干达"),
            }

            inserted = 0
            for country, city in sorted(unmatched_probes):
                loc = probe_locations.get((country, city), {})
                meta = country_meta.get(country)
                if meta:
                    country_en = meta["country_en"]
                    country_cn = meta["country_cn"]
                elif country in COUNTRY_NAMES:
                    country_en, country_cn = COUNTRY_NAMES[country]
                else:
                    logger.warning("  Skip %s/%s — no existing row or name mapping for country", country, city)
                    continue
                await conn.execute(
                    """INSERT INTO cities (continent, region, country_iso2, country_en, country_cn, city, probe_num)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                    loc.get("continent", ""),
                    loc.get("region", ""),
                    country,
                    country_en,
                    country_cn,
                    city,
                    probe_counts[(country, city)],
                )
                inserted += 1
                logger.info("  Inserted %s/%s (%d probes)", country, city, probe_counts[(country, city)])

            logger.info("Inserted %d new cities", inserted)

    return matched


async def main():
    async with aiohttp.ClientSession() as session:
        pool = await asyncpg.create_pool(
            host=DB_HOST, port=DB_PORT, user=DB_USER,
            password=DB_PASSWORD, database=DB_NAME,
            min_size=2, max_size=10,
        )
        matched = await update_probes(session, pool)
        await pool.close()

    # Regenerate cities.json after probe update (standalone run only)
    import subprocess
    subprocess.run(["python3", "gen_cities_json.py"], check=True)
    logger.info("Regenerated cities.json")

    logger.info("Done — %d cities updated", matched)


if __name__ == "__main__":
    asyncio.run(main())
