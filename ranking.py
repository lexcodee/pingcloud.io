"""Generate JSON ranking files from latency results."""

import argparse
import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import db
from config import DEFAULTS, load_config_file

logger = logging.getLogger(__name__)

# Per-city loss_pct threshold: cities with higher loss to a given endpoint
# are excluded from ranking aggregation (e.g. single unstable eyeball probes).
CITY_LOSS_THRESHOLD = 50.0


def _aggregate_cities(cities: list[dict]) -> dict:
    """Aggregate stats across multiple cities for the same group key.

    Uses test_count-weighted averages so the result represents the true
    mean across all individual test samples, not the mean of per-city means.
    """
    total_weight = sum(c["test_count"] for c in cities)
    w_median = sum(c["median_ms"] * c["test_count"] for c in cities)
    agg_median = round(w_median / total_weight, 1) if total_weight else 0
    w_avg = sum(c["avg_ms"] * c["test_count"] for c in cities if c["avg_ms"] is not None)
    w_avg_weight = sum(c["test_count"] for c in cities if c["avg_ms"] is not None)
    agg_avg = round(w_avg / w_avg_weight, 1) if w_avg_weight else None
    w_loss = sum(c["loss_pct"] * c["test_count"] for c in cities if c["loss_pct"] is not None)
    w_loss_weight = sum(c["test_count"] for c in cities if c["loss_pct"] is not None)
    agg_loss = round(w_loss / w_loss_weight, 2) if w_loss_weight else None
    return {
        "median_ms": agg_median,
        "avg_ms": agg_avg,
        "loss_pct": agg_loss,
        "city_count": len(set(c["city_id"] for c in cities)),
        "test_count": total_weight,
    }


def _bump_data_version() -> None:
    """Update __DATA_VERSION in index.html so the frontend picks up new data."""
    html_path = Path(__file__).resolve().parent / "web" / "static" / "index.html"
    if not html_path.exists():
        logger.warning("index.html not found at %s, skipping version bump", html_path)
        return
    new_ver = datetime.now(timezone.utc).strftime("%Y%m%d%H")
    content = html_path.read_text()
    updated, count = re.subn(
        r'var __DATA_VERSION=\d+;',
        f'var __DATA_VERSION={new_ver};',
        content,
    )
    if count:
        html_path.write_text(updated)
        logger.info("Bumped __DATA_VERSION to %s", new_ver)
    else:
        logger.warning("__DATA_VERSION not found in %s", html_path)


async def generate_rankings(output_dir: str, ranking_periods: list[int]):
    """Generate country_ranking and region_ranking JSON files for each period."""
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    generated_at = datetime.now(timezone.utc).isoformat()

    for days in ranking_periods:
        results = await db.fetch_results_for_period(days)
        if not results:
            logger.warning("No results for %d-day period, skipping", days)
            continue

        suffix = f"_p{days}d"

        # ── country_ranking: per vendor/region, top 20 countries by lowest median ──
        # Aggregate by (vendor, region_name, country_iso2) across cities
        vc_groups: dict[tuple[str, str, str], list[dict]] = {}
        skipped_high_loss = 0
        for r in results:
            if r.get("median_ms") is None:
                continue
            if (r.get("loss_pct") or 0) > CITY_LOSS_THRESHOLD:
                skipped_high_loss += 1
                continue
            key = (r["vendor"], r["region_name"], r["country_iso2"])
            vc_groups.setdefault(key, []).append({
                "city_id": r["city_id"],
                "country_en": r["country_en"],
                "median_ms": r["median_ms"],
                "avg_ms": r["avg_ms"],
                "loss_pct": r["loss_pct"],
                "test_count": r["test_count"],
            })
        if skipped_high_loss:
            logger.info("Period %dd: skipped %d city-endpoint rows with loss_pct > %.0f%%",
                        days, skipped_high_loss, CITY_LOSS_THRESHOLD)

        vr_entries: dict[tuple[str, str], list[dict]] = {}
        for (vendor, region_name, country_iso2), cities in vc_groups.items():
            agg = _aggregate_cities(cities)
            vr_entries.setdefault((vendor, region_name), []).append({
                "country_iso2": country_iso2,
                "country_en": cities[0]["country_en"],
                **agg,
            })

        country_rankings: dict[str, dict] = {}
        for (vendor, region_name), entries in vr_entries.items():
            entries.sort(key=lambda x: x["median_ms"])
            top = entries[:20]
            for i, e in enumerate(top):
                e["rank"] = i + 1
            country_rankings[f"{vendor}/{region_name}"] = {"entries": top}

        country_file = Path(output_dir) / f"country_ranking_{date_str}{suffix}.json"
        country_file.write_text(json.dumps({
            "generated_at": generated_at,
            "period_days": days,
            "rankings": country_rankings,
        }, ensure_ascii=False, indent=2))
        logger.info("Wrote %s (%d regions)", country_file.name, len(country_rankings))

        # ── region_ranking: per country, top 20 regions by lowest median ──
        # Aggregate by (country_iso2, vendor, region_name) across cities
        cvr_groups: dict[tuple[str, str, str], list[dict]] = {}
        for r in results:
            if r.get("median_ms") is None:
                continue
            if (r.get("loss_pct") or 0) > CITY_LOSS_THRESHOLD:
                continue
            key = (r["country_iso2"], r["vendor"], r["region_name"])
            cvr_groups.setdefault(key, []).append({
                "city_id": r["city_id"],
                "median_ms": r["median_ms"],
                "avg_ms": r["avg_ms"],
                "loss_pct": r["loss_pct"],
                "test_count": r["test_count"],
            })

        country_entries: dict[str, list[dict]] = {}
        for (country_iso2, vendor, region_name), cities in cvr_groups.items():
            agg = _aggregate_cities(cities)
            country_entries.setdefault(country_iso2, []).append({
                "vendor": vendor,
                "region_name": region_name,
                **agg,
            })

        region_rankings: dict[str, dict] = {}
        for country_iso2, entries in country_entries.items():
            entries.sort(key=lambda x: x["median_ms"])
            top = entries[:20]
            for i, e in enumerate(top):
                e["rank"] = i + 1
            region_rankings[country_iso2] = {"entries": top}

        region_file = Path(output_dir) / f"region_ranking_{date_str}{suffix}.json"
        region_file.write_text(json.dumps({
            "generated_at": generated_at,
            "period_days": days,
            "rankings": region_rankings,
        }, ensure_ascii=False, indent=2))
        logger.info("Wrote %s (%d countries)", region_file.name, len(region_rankings))

        # ── geo_region_ranking: per geographic region, top 20 vendor regions ──
        # For each geographic region, collect ALL entries per country (untruncated),
        # intersect (vendor, region_name) across all countries, aggregate, keep top 20.
        # IMPORTANT: intersection must be computed on full data (country_entries), not
        # the already-truncated region_rankings (top 20), otherwise vendor/regions that
        # barely miss top 20 in some countries are incorrectly excluded from the
        # intersection, causing shorter periods to paradoxically have more entries.
        # Fetch country → geographic region mapping from DB
        country_region_map = await db.fetch_country_region_map()
        # Group countries by geographic region
        geo_region_countries: dict[str, list[str]] = {}
        for iso2, geo_region in country_region_map.items():
            geo_region_countries.setdefault(geo_region, []).append(iso2)

        geo_region_rankings: dict[str, dict] = {}
        for geo_region, iso2_list in geo_region_countries.items():
            # Collect ALL entries per country that has data
            # Use country_entries (untruncated) instead of region_rankings (top 20)
            # so that the majority intersection is computed on complete data.
            # Truncation to top 20 happens only at the final geo_region level.
            country_all: dict[str, list[dict]] = {}
            for iso2 in iso2_list:
                if iso2 in country_entries:
                    country_all[iso2] = country_entries[iso2]
            if len(country_all) < 1:
                continue

            # Build majority intersection: vendor/region must appear in ≥50% of countries
            from collections import Counter
            key_counter: Counter = Counter()
            for entries in country_all.values():
                keys = set(f"{e['vendor']}|{e['region_name']}" for e in entries)
                key_counter.update(keys)
            total_countries = len(country_all)
            threshold = max(1, (total_countries + 1) // 2)  # majority: >50%
            intersection_keys = {k for k, cnt in key_counter.items() if cnt >= threshold}
            if not intersection_keys:
                continue

            # Aggregate intersection entries across countries
            group_map: dict[str, dict] = {}
            for iso2, entries in country_all.items():
                for e in entries:
                    key = f"{e['vendor']}|{e['region_name']}"
                    if key not in intersection_keys:
                        continue
                    if key not in group_map:
                        group_map[key] = {
                            "vendor": e["vendor"],
                            "region_name": e["region_name"],
                            "_avg": [],
                            "_median": [],
                            "_loss": [],
                            "city_count": 0,
                            "test_count": 0,
                        }
                    if e.get("avg_ms") is not None:
                        group_map[key]["_avg"].append(e["avg_ms"])
                    if e.get("median_ms") is not None:
                        group_map[key]["_median"].append(e["median_ms"])
                    if e.get("loss_pct") is not None:
                        group_map[key]["_loss"].append(e["loss_pct"])
                    group_map[key]["city_count"] += e.get("city_count", 0)
                    group_map[key]["test_count"] += e.get("test_count", 0)

            def _mean(vals: list[float]) -> float | None:
                return round(sum(vals) / len(vals), 1) if vals else None

            agg_entries = []
            for g in group_map.values():
                agg_entries.append({
                    "vendor": g["vendor"],
                    "region_name": g["region_name"],
                    "avg_ms": _mean(g["_avg"]),
                    "median_ms": _mean(g["_median"]),
                    "loss_pct": _mean(g["_loss"]),
                    "city_count": g["city_count"],
                    "test_count": g["test_count"],
                })

            agg_entries.sort(key=lambda x: x["median_ms"] if x["median_ms"] is not None else float("inf"))
            top = agg_entries[:20]
            for i, e in enumerate(top):
                e["rank"] = i + 1
            geo_region_rankings[geo_region] = {"entries": top}

        geo_region_file = Path(output_dir) / f"geo_region_ranking_{date_str}{suffix}.json"
        geo_region_file.write_text(json.dumps({
            "generated_at": generated_at,
            "period_days": days,
            "rankings": geo_region_rankings,
        }, ensure_ascii=False, indent=2))
        logger.info("Wrote %s (%d geographic regions)", geo_region_file.name, len(geo_region_rankings))

    # Remove old ranking files for any period (keep only latest date)
    out = Path(output_dir)
    for pattern in ("country_ranking_*", "region_ranking_*", "geo_region_ranking_*"):
        for f in sorted(out.glob(pattern)):
            if date_str not in f.name:
                f.unlink()
                logger.info("Removed old ranking file: %s", f.name)

    # Write periods manifest so frontend can discover available periods
    periods_file = out / "periods.json"
    periods_file.write_text(json.dumps({
        "periods": ranking_periods,
        "generated_at": generated_at,
    }))
    logger.info("Wrote periods.json with periods=%s", ranking_periods)

    _bump_data_version()

    # Rebuild i18n HTML so index.en.html / index.zh.html pick up the new __DATA_VERSION
    try:
        from build_i18n import build as build_i18n
        build_i18n()
        logger.info("Rebuilt i18n HTML files with updated __DATA_VERSION")
    except Exception as e:
        logger.warning("Failed to rebuild i18n HTML: %s", e)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ranking JSON files from latency results")
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument("--output-dir", default=None, help="Output directory for ranking files")
    parser.add_argument("--ranking-periods", type=int, nargs="+", default=None,
                        help="Ranking aggregation periods in days (e.g. 1 7 15)")
    args = parser.parse_args()
    file_cfg = load_config_file(args.config)
    args.output_dir = args.output_dir or file_cfg.get("output_dir", DEFAULTS["output_dir"])
    args.ranking_periods = args.ranking_periods or file_cfg.get("ranking_periods", DEFAULTS["ranking_periods"])
    return args


async def _main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    args = _parse_args()
    logger.info("Generating rankings: periods=%s, output_dir=%s", args.ranking_periods, args.output_dir)
    await generate_rankings(args.output_dir, args.ranking_periods)
    await db.close_pool()
    logger.info("Done")


if __name__ == "__main__":
    asyncio.run(_main())
