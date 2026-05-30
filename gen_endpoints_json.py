"""Generate endpoints.json from the cloud_endpoints database table.

Output format (one object per endpoint row):
  {"id":1,"vendor":"AWS","region_name":"Cape Town (ZA)","region_id":"af-south-1","endpoint":"ec2.af-south-1.amazonaws.com"}

Usage:
  python3 gen_endpoints_json.py                  # write to web/static/data/endpoints.json
  python3 gen_endpoints_json.py -o /tmp/e.json   # custom output path
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
DB_NAME = "pingcloud_dev"

QUERY = """
SELECT id, vendor, region_name, region_id, endpoint
FROM cloud_endpoints
ORDER BY id
"""


async def generate(output: Path) -> None:
    conn = await asyncpg.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
    )
    rows = await conn.fetch(QUERY)
    await conn.close()

    data = [
        {
            "id": r["id"],
            "vendor": r["vendor"],
            "region_name": r["region_name"],
            "region_id": r["region_id"] or "",
            "endpoint": r["endpoint"] or "",
        }
        for r in rows
    ]

    output.parent.mkdir(parents=True, exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Wrote {len(data)} endpoints to {output}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate endpoints.json from DB")
    parser.add_argument("-o", "--output", default="web/static/data/endpoints.json", help="Output file path")
    args = parser.parse_args()
    asyncio.run(generate(Path(args.output)))


if __name__ == "__main__":
    main()
