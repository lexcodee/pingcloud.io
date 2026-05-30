"""Generate cities.json from the cities database table.

Output format (one object per city row):
  {"id": 3, "region": "Northern America", "en": "United States", "cn": "美国", "iso2": "US", "city": "Atlanta", "city_cn": "亚特兰大", "probe_num": 5}

Usage:
  python3 gen_cities_json.py                  # write to web/static/data/cities.json
  python3 gen_cities_json.py -o /tmp/c.json   # custom output path
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import asyncpg

DB_HOST = "localhost"
DB_PORT = 5432
DB_USER = "postgres"
DB_PASSWORD = "22223333"
DB_NAME = "postgres"

QUERY = """
SELECT id, continent, region, country_en, country_cn, country_iso2, city, city_cn, probe_num
FROM cities
WHERE probe_num > 0
ORDER BY id
"""

CONTINENT_NORMALIZE = {
    "AF": "Africa", "AS": "Asia", "EU": "Europe",
    "NA": "North America", "SA": "South America", "OC": "Oceania",
}


async def generate(output: Path) -> None:
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
    )
    rows = await conn.fetch(QUERY)
    await conn.close()

    data = [
        {
            "id": r["id"],
            "continent": CONTINENT_NORMALIZE.get(r["continent"], r["continent"]),
            "region": r["region"],
            "en": r["country_en"],
            "cn": r["country_cn"],
            "iso2": r["country_iso2"],
            "city": r["city"],
            "city_cn": r["city_cn"] or "",
            "probe_num": r["probe_num"],
        }
        for r in rows
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(data)} cities to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate cities.json from DB")
    parser.add_argument("-o", "--output", default="web/static/data/cities.json", help="Output file path")
    args = parser.parse_args()
    asyncio.run(generate(Path(args.output)))


if __name__ == "__main__":
    main()
