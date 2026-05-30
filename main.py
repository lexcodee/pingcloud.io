"""Entry point: parse args, setup logging, dispatch mode."""

import asyncio
import logging
import sys

import db
import ranking
import scheduler
from config import parse_args


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def main():
    args = parse_args()
    setup_logging()
    logger = logging.getLogger("main")

    logger.info("Starting mode=%s", args.mode)
    await db.ensure_tables()

    if args.mode == "init":
        await scheduler.run_init(
            city_concurrency=args.city_concurrency,
            endpoint_concurrency=args.endpoint_concurrency,
            hourly_limit=args.hourly_limit,
            globalping_mode=args.globalping_mode,
            max_cities_per_country=args.max_cities_per_country,
        )
        await ranking.generate_rankings(args.output_dir, args.ranking_periods)

    elif args.mode == "refresh":
        await scheduler.run_refresh(
            city_concurrency=args.city_concurrency,
            endpoint_concurrency=args.endpoint_concurrency,
            hourly_limit=args.hourly_limit,
            rank_high_threshold=args.rank_high_threshold,
            rank_mid_threshold=args.rank_mid_threshold,
            rank_high_interval_h=args.rank_high_interval_h,
            rank_mid_interval_h=args.rank_mid_interval_h,
            rank_low_interval_days=args.rank_low_interval_days,
            globalping_mode=args.globalping_mode,
            max_cities_per_country=args.max_cities_per_country,
        )
        await ranking.generate_rankings(args.output_dir, args.ranking_periods)

    await db.close_pool()
    logger.info("Done")


if __name__ == "__main__":
    asyncio.run(main())
