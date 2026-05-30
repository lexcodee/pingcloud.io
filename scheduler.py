"""Concurrency scheduler for init and refresh modes."""

import asyncio
import logging
import random
import time
from datetime import datetime, timezone, timedelta

import aiohttp

import db
import globalping
from globalping import NoProbesFoundError, ProbeUnreachableError, DirectRateLimitError
from ranking import _aggregate_cities, CITY_LOSS_THRESHOLD
from proxy_manager import OxylabsProxyManager
from config import (
    GLOBALPING_API_URL,
    OXYLABS_PROXY_HOST, OXYLABS_USERNAME, OXYLABS_PASSWORD,
    OXYLABS_PORT_MIN, OXYLABS_PORT_MAX, OXYLABS_MAX_USES_PER_PORT,
)

logger = logging.getLogger(__name__)


# ── Quota gate ──────────────────────────────────────────────────────────

async def wait_for_quota(hourly_limit: int):
    """Block until a quota slot is available."""
    while True:
        if await db.check_and_consume_quota(hourly_limit):
            return
        # Calculate seconds until next hour boundary
        now = datetime.now(timezone.utc)
        next_hour = (now.replace(minute=0, second=0, microsecond=0)
                     + timedelta(hours=1))
        wait_s = (next_hour - now).total_seconds() + 1
        logger.warning("Hourly quota exhausted, waiting %.0fs until next hour", wait_s)
        await asyncio.sleep(wait_s)


async def _call_with_quota_guard(hourly_limit: int, proxy, coro_fn):
    """Call coro_fn with quota guard. On DirectRateLimitError (429), force
    exhaust DB quota so all concurrent tasks also block, then wait until next hour."""
    while True:
        try:
            if not proxy:
                await wait_for_quota(hourly_limit)
            return await coro_fn()
        except DirectRateLimitError:
            await db.force_exhaust_quota(hourly_limit)
            now = datetime.now(timezone.utc)
            next_hour = (now.replace(minute=0, second=0, microsecond=0)
                         + timedelta(hours=1))
            wait_s = (next_hour - now).total_seconds() + 1
            logger.warning("Direct mode 429 received — quota exhausted, waiting %.0fs until next hour",
                           wait_s)
            await asyncio.sleep(wait_s)


# ── City selection ──────────────────────────────────────────────────────

def _select_cities_by_country(
    cities: list[dict],
    max_cities_per_country: int,
) -> list[dict]:
    """Limit cities per country using probe-weighted random sampling.

    For each country with more than max_cities_per_country cities that have
    probes (probe_num > 0), selects up to N cities using weighted random
    sampling where weight = probe_num. Cities with more probes are more
    likely to be selected.

    Cities with probe_num == 0 are always excluded.
    """
    # Group cities by country
    by_country: dict[str, list[dict]] = {}
    for city in cities:
        if city.get("probe_num", 0) == 0:
            continue
        by_country.setdefault(city["country_iso2"], []).append(city)

    selected: list[dict] = []
    total_skipped = 0
    for country_iso2, country_cities in sorted(by_country.items()):
        if len(country_cities) <= max_cities_per_country:
            selected.extend(country_cities)
            continue

        # Weighted random sampling: weight = probe_num
        weights = [c.get("probe_num", 1) for c in country_cities]
        chosen = random.choices(country_cities, weights=weights, k=max_cities_per_country)
        # Deduplicate (random.choices can pick same item multiple times)
        seen_ids: set[int] = set()
        unique_chosen: list[dict] = []
        for c in chosen:
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                unique_chosen.append(c)

        # If deduplication reduced count below max, fill from remaining
        remaining = [c for c in country_cities if c["id"] not in seen_ids]
        if remaining and len(unique_chosen) < max_cities_per_country:
            remaining.sort(key=lambda c: c.get("probe_num", 0), reverse=True)
            for c in remaining:
                if len(unique_chosen) >= max_cities_per_country:
                    break
                unique_chosen.append(c)
                seen_ids.add(c["id"])

        skipped = len(country_cities) - len(unique_chosen)
        total_skipped += skipped
        logger.info("Country %s: %d cities with probes, selected %d (skipped %d, weighted by probe_num)",
                    country_iso2, len(country_cities), len(unique_chosen), skipped)
        selected.extend(unique_chosen)

    if total_skipped:
        logger.info("City selection: %d cities selected, %d skipped across countries exceeding limit",
                    len(selected), total_skipped)
    else:
        logger.info("City selection: %d cities selected (no country exceeded max of %d)",
                    len(selected), max_cities_per_country)

    # Sort selected cities by probe_num descending so highest-probe cities are tested first
    selected.sort(key=lambda c: c.get("probe_num", 0), reverse=True)
    return selected


# ── Init mode ───────────────────────────────────────────────────────────

async def run_init(
    city_concurrency: int,
    endpoint_concurrency: int,
    hourly_limit: int,
    globalping_mode: str = "direct",
    max_cities_per_country: int = 20,
):
    proxy = None
    if globalping_mode == "proxy":
        proxy = OxylabsProxyManager(
            host=OXYLABS_PROXY_HOST,
            username=OXYLABS_USERNAME,
            password=OXYLABS_PASSWORD,
            port_min=OXYLABS_PORT_MIN,
            port_max=OXYLABS_PORT_MAX,
            max_uses_per_port=OXYLABS_MAX_USES_PER_PORT,
            pool_size=city_concurrency,
        )
        logger.info("Using Oxylabs DC proxy pool (%d slots, ports %d-%d, %d uses/port)",
                    city_concurrency, OXYLABS_PORT_MIN, OXYLABS_PORT_MAX, OXYLABS_MAX_USES_PER_PORT)

    cities = await db.fetch_all_cities()
    cities = _select_cities_by_country(cities, max_cities_per_country)
    endpoints = await db.fetch_all_endpoints()
    existing = await db.fetch_existing_pairs()

    if not cities:
        logger.error("No cities found in database")
        return
    if not endpoints:
        logger.error("No cloud_endpoints found in database")
        return

    total = len(cities) * len(endpoints)
    done = len(existing)
    logger.info("Init mode: %d cities × %d endpoints = %d total, %d already done",
                len(cities), len(endpoints), total, done)

    # Build work items: (city, [endpoints not yet tested for this city])
    work: list[tuple[dict, list[dict]]] = []
    for city in cities:
        remaining = [ep for ep in endpoints
                     if (city["id"], ep["id"]) not in existing]
        if remaining:
            work.append((city, remaining))

    if not work:
        logger.info("All city×endpoint pairs already tested, nothing to do")
        return

    # Shared aiohttp session
    async with aiohttp.ClientSession() as session:
        city_sem = asyncio.Semaphore(city_concurrency)
        progress = _ProgressTracker(len(work))

        async def process_city(city: dict, eps: list[dict], slot: int = 0):
            async with city_sem:
                await _process_city_endpoints(
                    session, city, eps, endpoint_concurrency, hourly_limit, progress,
                    proxy=proxy, slot=slot,
                )

        pool_size = proxy.pool_size if proxy else 1
        tasks = [
            asyncio.create_task(process_city(c, eps, slot=i % pool_size))
            for i, (c, eps) in enumerate(work)
        ]
        await asyncio.gather(*tasks)

    logger.info("Init mode complete")


async def _process_city_endpoints(
    session: aiohttp.ClientSession,
    city: dict,
    endpoints: list[dict],
    ep_concurrency: int,
    hourly_limit: int,
    progress: "_ProgressTracker",
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
):
    city_label = f"{city['country_en']}/{city['city']}"
    ep_sem = asyncio.Semaphore(ep_concurrency)
    anchor: str | None = None

    # First endpoint: sequential, establishes probe anchor
    first_ep = endpoints[0]
    try:
        result = await _call_with_quota_guard(
            hourly_limit, proxy,
            lambda: globalping.ping_with_city(
                session, first_ep["endpoint"],
                city["country_iso2"], city["city"],
                proxy=proxy, slot=slot,
            ),
        )
        anchor = result.measurement_id
        await db.insert_result(
            city_id=city["id"], endpoint_id=first_ep["id"],
            measurement_id=result.measurement_id,
            avg_ms=result.avg_ms, median_ms=result.median_ms,
            loss_pct=result.loss_pct, probe_ip=result.probe_ip,
            mdev_ms=result.mdev_ms, test_round="init",
        )
        await db.mark_city_probe_online(city["id"])
        progress.log(city_label, first_ep["endpoint"], result.avg_ms, result.loss_pct)
    except NoProbesFoundError as e:
        logger.warning("City %s has no available probes, skipping all %d endpoints: %s",
                       city_label, len(endpoints), e)
        await db.set_probe_num(city["id"], 0)
        await progress.city_done()
        return
    except ProbeUnreachableError as e:
        logger.warning("City %s first endpoint %s unreachable from probe: %s",
                       city_label, first_ep["endpoint"], e)
        progress.log(city_label, first_ep["endpoint"], None)
    except Exception as e:
        logger.error("City %s first endpoint %s failed: %s", city_label, first_ep["endpoint"], e)
        progress.log(city_label, first_ep["endpoint"], None)

    # Remaining endpoints: reuse anchor if available, concurrency-limited
    if len(endpoints) > 1:
        remaining = endpoints[1:]

        async def test_one(ep: dict):
            nonlocal anchor
            try:
                if anchor:
                    result = await _call_with_quota_guard(
                        hourly_limit, proxy,
                        lambda: globalping.ping_with_probe(
                            session, ep["endpoint"], anchor,
                            proxy=proxy, slot=slot,
                        ),
                    )
                else:
                    # Fallback: locate probe by city again
                    result = await _call_with_quota_guard(
                        hourly_limit, proxy,
                        lambda: globalping.ping_with_city(
                            session, ep["endpoint"],
                            city["country_iso2"], city["city"],
                            proxy=proxy, slot=slot,
                        ),
                    )
                await db.insert_result(
                    city_id=city["id"], endpoint_id=ep["id"],
                    measurement_id=result.measurement_id,
                    avg_ms=result.avg_ms, median_ms=result.median_ms,
                    loss_pct=result.loss_pct, probe_ip=result.probe_ip,
                    mdev_ms=result.mdev_ms, test_round="init",
                )
                await db.mark_city_probe_online(city["id"])
                progress.log(city_label, ep["endpoint"], result.avg_ms, result.loss_pct)
            except ProbeUnreachableError as e:
                logger.warning("City %s endpoint %s unreachable from probe: %s",
                               city_label, ep["endpoint"], e)
                progress.log(city_label, ep["endpoint"], "UNREACHABLE")
            except Exception as e:
                logger.error("City %s endpoint %s failed: %s", city_label, ep["endpoint"], e)
                progress.log(city_label, ep["endpoint"], "FAIL")

        async def bounded_test(ep: dict):
            async with ep_sem:
                await test_one(ep)

        await asyncio.gather(*[asyncio.create_task(bounded_test(ep)) for ep in remaining])

    await progress.city_done()


class _ProgressTracker:
    def __init__(self, total_cities: int):
        self.total = total_cities
        self.completed = 0
        self._lock = asyncio.Lock()

    def log(self, city: str, target: str, latency, loss_pct: float | None = None):
        if isinstance(latency, str):
            label = latency
        elif latency is None:
            label = f"loss {loss_pct:.0f}%" if loss_pct is not None else "N/A"
        else:
            label = f"{latency:.1f} ms"
        logger.info("  %-40s → %-40s %s", city, target, label)

    async def city_done(self):
        async with self._lock:
            self.completed += 1
            logger.info("City progress: %d/%d completed", self.completed, self.total)


class _RefreshProgress:
    """Thread-safe progress tracker for refresh queues."""

    def __init__(self, round_name: str, total: int):
        self.round_name = round_name
        self.total = total
        self.done = 0
        self._lock = asyncio.Lock()
        self._start = time.monotonic()

    async def tick(self, city_label: str, endpoint: str, result):
        """Record one completed test and log progress line."""
        async with self._lock:
            self.done += 1
            elapsed = time.monotonic() - self._start
            pct = self.done / self.total * 100 if self.total else 0
            if self.done < self.total and elapsed > 0:
                eta = elapsed / self.done * (self.total - self.done)
                eta_s = f"{int(eta // 60)}m{int(eta % 60):02d}s"
            else:
                eta_s = "—"
            label = self._result_label(result)
            logger.info(
                "  [%s %d/%d %.0f%% ETA %s] %s → %s: %s",
                self.round_name, self.done, self.total, pct, eta_s,
                city_label, endpoint, label,
            )

    async def skip(self, n: int):
        """Record n tests skipped (e.g. NoProbesFound for remaining endpoints)."""
        async with self._lock:
            self.done += n

    def summary(self):
        """Log final summary."""
        elapsed = time.monotonic() - self._start
        m, s = divmod(int(elapsed), 60)
        logger.info(
            "%s complete: %d/%d tests, elapsed %dm%02ds",
            self.round_name, self.done, self.total, m, s,
        )

    @staticmethod
    def _result_label(result) -> str:
        if result is None:
            return "FAIL"
        if isinstance(result, str):
            return result  # "UNREACHABLE"
        if hasattr(result, "avg_ms") and result.avg_ms is not None:
            return f"{result.avg_ms:.1f} ms"
        if hasattr(result, "loss_pct") and result.loss_pct is not None:
            return f"loss {result.loss_pct:.0f}%"
        return "N/A"


# ── Refresh mode ────────────────────────────────────────────────

def _build_refresh_queues(
    results: list[dict],
    endpoints: list[dict],
    rank_high_threshold: int,
    rank_mid_threshold: int,
) -> tuple[set[tuple[str, int]], set[tuple[str, int]]]:
    """Build HIGH and MID refresh queues from 30-day ranking data.

    Country ranking: per (vendor, region), rank countries by median
      → top N = HIGH, next M = MID.
    Region ranking: per country, rank (vendor, region) by median
      → top N = HIGH, next M = MID.
    Merge: HIGH overrides MID; overlapping pairs tested only once.
    Pairs outside top M in both rankings are skipped (manual only).

    Returns (high_pairs, mid_pairs) where each pair is (country_iso2, endpoint_id).
    """
    # Map (vendor, region_name) → set of endpoint_ids
    vr_to_ep_ids: dict[tuple[str, str], set[int]] = {}
    for ep in endpoints:
        key = (ep["vendor"], ep["region_name"])
        vr_to_ep_ids.setdefault(key, set()).add(ep["id"])

    # ── Country ranking ──
    # Group by (vendor, region_name, country_iso2), aggregate across cities
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
            "median_ms": r["median_ms"],
            "avg_ms": r["avg_ms"],
            "loss_pct": r["loss_pct"],
            "test_count": r["test_count"],
        })

    # For each (vendor, region_name), rank countries by aggregated median
    country_high: set[tuple[str, int]] = set()
    country_mid: set[tuple[str, int]] = set()
    vr_countries: dict[tuple[str, str], list[dict]] = {}
    for (vendor, region_name, country_iso2), cities in vc_groups.items():
        agg = _aggregate_cities(cities)
        vr_countries.setdefault((vendor, region_name), []).append({
            "country_iso2": country_iso2,
            "median_ms": agg["median_ms"],
        })

    for (vendor, region_name), entries in vr_countries.items():
        entries.sort(key=lambda x: x["median_ms"])
        ep_ids = vr_to_ep_ids.get((vendor, region_name), set())
        for i, entry in enumerate(entries):
            for ep_id in ep_ids:
                pair = (entry["country_iso2"], ep_id)
                if i < rank_high_threshold:
                    country_high.add(pair)
                elif i < rank_mid_threshold:
                    country_mid.add(pair)

    # ── Region ranking ──
    # Group by (country_iso2, vendor, region_name), aggregate across cities
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

    # For each country, rank (vendor, region_name) by aggregated median
    region_high: set[tuple[str, int]] = set()
    region_mid: set[tuple[str, int]] = set()
    country_vrs: dict[str, list[dict]] = {}
    for (country_iso2, vendor, region_name), cities in cvr_groups.items():
        agg = _aggregate_cities(cities)
        country_vrs.setdefault(country_iso2, []).append({
            "vendor": vendor,
            "region_name": region_name,
            "median_ms": agg["median_ms"],
        })

    for country_iso2, entries in country_vrs.items():
        entries.sort(key=lambda x: x["median_ms"])
        for i, entry in enumerate(entries):
            vr_key = (entry["vendor"], entry["region_name"])
            ep_ids = vr_to_ep_ids.get(vr_key, set())
            for ep_id in ep_ids:
                pair = (country_iso2, ep_id)
                if i < rank_high_threshold:
                    region_high.add(pair)
                elif i < rank_mid_threshold:
                    region_mid.add(pair)

    # ── Merge: HIGH overrides MID ──
    high_pairs = country_high | region_high
    mid_pairs = (country_mid | region_mid) - high_pairs

    if skipped_high_loss:
        logger.info("Skipped %d city-endpoint rows with loss_pct > %.0f%%",
                    skipped_high_loss, CITY_LOSS_THRESHOLD)
    logger.info("Country ranking: HIGH=%d, MID=%d pairs",
                len(country_high), len(country_mid))
    logger.info("Region ranking: HIGH=%d, MID=%d pairs",
                len(region_high), len(region_mid))
    logger.info("Merged: HIGH=%d, MID=%d pairs",
                len(high_pairs), len(mid_pairs))

    return high_pairs, mid_pairs


def _expand_to_stale_city_pairs(
    pairs: set[tuple[str, int]],
    city_map: dict,
    latest_results: list[dict],
    interval: timedelta,
    max_cities_per_country: int = 20,
) -> dict[int, list[int]]:
    """Expand (country_iso2, endpoint_id) pairs to stale (city_id, endpoint_id) pairs.

    Returns stale_by_city mapping city_id → list of stale endpoint_ids.
    """
    now = datetime.now(timezone.utc)

    # Build lookup: country_iso2 → list of city_ids
    # Apply per-country city limit with probe-weighted selection
    country_cities: dict[str, list[int]] = {}
    by_country: dict[str, list[dict]] = {}
    for c in city_map.values():
        if c.get("probe_num", 0) == 0:
            continue
        by_country.setdefault(c["country_iso2"], []).append(c)

    for country_iso2, cc in by_country.items():
        if len(cc) <= max_cities_per_country:
            country_cities[country_iso2] = [c["id"] for c in cc]
            continue
        # Weighted random sampling
        weights = [c.get("probe_num", 1) for c in cc]
        chosen = random.choices(cc, weights=weights, k=max_cities_per_country)
        seen_ids: set[int] = set()
        unique: list[int] = []
        for c in chosen:
            if c["id"] not in seen_ids:
                seen_ids.add(c["id"])
                unique.append(c["id"])
        remaining = [c for c in cc if c["id"] not in seen_ids]
        if remaining and len(unique) < max_cities_per_country:
            remaining.sort(key=lambda c: c.get("probe_num", 0), reverse=True)
            for c in remaining:
                if len(unique) >= max_cities_per_country:
                    break
                unique.append(c["id"])
                seen_ids.add(c["id"])
        skipped = len(cc) - len(unique)
        logger.info("Refresh city selection: country %s has %d cities with probes, selected %d (skipped %d)",
                    country_iso2, len(cc), len(unique), skipped)
        country_cities[country_iso2] = unique

    # Build lookup: (city_id, endpoint_id) → latest tested_at
    tested_at_map: dict[tuple[int, int], datetime] = {}
    for r in latest_results:
        key = (r["city_id"], r["endpoint_id"])
        ta = r.get("tested_at")
        if ta and (key not in tested_at_map or ta > tested_at_map[key]):
            tested_at_map[key] = ta

    # Collect stale (city_id, endpoint_id) pairs, grouped by city
    # (probe_num=0 cities already excluded from country_cities)
    stale_by_city: dict[int, list[int]] = {}
    for (country_iso2, endpoint_id) in pairs:
        for city_id in country_cities.get(country_iso2, []):
            last = tested_at_map.get((city_id, endpoint_id))
            if last is None or (now - last.replace(tzinfo=timezone.utc)) > interval:
                stale_by_city.setdefault(city_id, []).append(endpoint_id)

    return stale_by_city


async def run_refresh(
    city_concurrency: int,
    endpoint_concurrency: int,
    hourly_limit: int,
    rank_high_threshold: int,
    rank_mid_threshold: int,
    rank_high_interval_h: int,
    rank_mid_interval_h: int,
    rank_low_interval_days: int,
    globalping_mode: str = "direct",
    max_cities_per_country: int = 20,
):
    """Continuous refresh loop: re-test based on ranking priority."""
    proxy = None
    if globalping_mode == "proxy":
        proxy = OxylabsProxyManager(
            host=OXYLABS_PROXY_HOST,
            username=OXYLABS_USERNAME,
            password=OXYLABS_PASSWORD,
            port_min=OXYLABS_PORT_MIN,
            port_max=OXYLABS_PORT_MAX,
            max_uses_per_port=OXYLABS_MAX_USES_PER_PORT,
            pool_size=city_concurrency,
        )
        logger.info("Using Oxylabs DC proxy pool (%d slots, ports %d-%d, %d uses/port)",
                    city_concurrency, OXYLABS_PORT_MIN, OXYLABS_PORT_MAX, OXYLABS_MAX_USES_PER_PORT)

    # Fetch 30-day aggregated results for ranking
    results = await db.fetch_results_for_period(30)
    if not results:
        logger.error("No results for 30-day period — run init first")
        return

    # Fetch latest results for staleness checking
    latest_results = await db.fetch_latest_results()

    endpoints = await db.fetch_all_endpoints()
    high_pairs, mid_pairs = _build_refresh_queues(
        results, endpoints,
        rank_high_threshold, rank_mid_threshold,
    )

    cities = await db.fetch_all_cities()
    city_map = {c["id"]: c for c in cities}
    ep_map = {e["id"]: e for e in endpoints}

    # Expand country-level pairs to city-level stale pairs upfront,
    # so the logged count matches the actual number of tests.
    queue_specs = [
        ("refresh_high", high_pairs, timedelta(hours=rank_high_interval_h)),
        ("refresh_mid", mid_pairs, timedelta(hours=rank_mid_interval_h)),
    ]
    queues: list[tuple[str, dict[int, list[int]], timedelta]] = []
    for round_name, pairs, interval in queue_specs:
        if not pairs:
            logger.info("Queue %s: no pairs, skipping", round_name)
            continue

        stale_by_city = _expand_to_stale_city_pairs(
            pairs, city_map, latest_results, interval,
            max_cities_per_country=max_cities_per_country,
        )
        total_stale = sum(len(eps) for eps in stale_by_city.values())

        if not total_stale:
            logger.info("Queue %s: no stale pairs", round_name)
            continue

        logger.info("Queue %s: %d stale pairs across %d cities, interval %s",
                    round_name, total_stale, len(stale_by_city), interval)
        queues.append((round_name, stale_by_city, interval))

    async with aiohttp.ClientSession() as session:
        for round_name, stale_by_city, interval in queues:
            # Timestamped test_round so each refresh cycle inserts new rows
            # instead of conflicting with previous cycle's rows.
            # e.g. "refresh_high" → "rh_20260531_0450"
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
            prefix = "rh" if round_name == "refresh_high" else "rm"
            test_round = f"{prefix}_{ts}"
            logger.info("Using test_round=%s for queue %s", test_round, round_name)
            await _run_refresh_queue(
                session, round_name, test_round, stale_by_city, interval,
                city_map, ep_map,
                city_concurrency, endpoint_concurrency, hourly_limit,
                proxy=proxy,
            )

    logger.info("Refresh mode complete")


async def _run_refresh_queue(
    session: aiohttp.ClientSession,
    round_name: str,
    test_round: str,
    stale_by_city: dict[int, list[int]],
    interval: timedelta,
    city_map: dict,
    ep_map: dict,
    city_concurrency: int,
    endpoint_concurrency: int,
    hourly_limit: int,
    proxy: OxylabsProxyManager | None = None,
):
    """Process one priority queue: re-test stale city-endpoint pairs."""
    total = sum(len(eps) for eps in stale_by_city.values())
    progress = _RefreshProgress(round_name, total)
    city_sem = asyncio.Semaphore(city_concurrency)

    async def retest_city(city_id: int, endpoint_ids: list[int], slot: int = 0):
        """Re-test one city's stale endpoints with probe anchor reuse."""
        async with city_sem:
            city = city_map[city_id]
            city_label = f"{city['country_en']}/{city['city']}"
            anchor: str | None = None
            ep_sem = asyncio.Semaphore(endpoint_concurrency)

            # First endpoint: sequential, establishes probe anchor
            first_eid = endpoint_ids[0]
            first_ep = ep_map[first_eid]
            try:
                result = await _call_with_quota_guard(
                    hourly_limit, proxy,
                    lambda: globalping.ping_with_city(
                        session, first_ep["endpoint"],
                        city["country_iso2"], city["city"],
                        proxy=proxy, slot=slot,
                    ),
                )
                anchor = result.measurement_id
                await db.insert_result(
                    city_id=city_id, endpoint_id=first_eid,
                    measurement_id=result.measurement_id,
                    avg_ms=result.avg_ms, median_ms=result.median_ms,
                    loss_pct=result.loss_pct, probe_ip=result.probe_ip,
                    mdev_ms=result.mdev_ms, test_round=test_round,
                )
                await db.mark_city_probe_online(city_id)
                await progress.tick(city_label, first_ep["endpoint"], result)
            except NoProbesFoundError as e:
                await db.set_probe_num(city_id, 0)
                await progress.skip(len(endpoint_ids))
                logger.warning("  %s: no available probes, skipping %d endpoints: %s",
                               city_label, len(endpoint_ids), e)
                return
            except ProbeUnreachableError as e:
                await progress.tick(city_label, first_ep["endpoint"], "UNREACHABLE")
                logger.warning("  %s → %s unreachable from probe: %s",
                               city_label, first_ep["endpoint"], e)
            except Exception as e:
                await progress.tick(city_label, first_ep["endpoint"], None)
                logger.error("  %s → %s failed: %s",
                             city_label, first_ep["endpoint"], e)

            # Remaining endpoints: reuse anchor, concurrency-limited
            if len(endpoint_ids) > 1:
                remaining_eids = endpoint_ids[1:]

                async def retest_one(eid: int):
                    nonlocal anchor
                    ep = ep_map[eid]
                    try:
                        if anchor:
                            result = await _call_with_quota_guard(
                                hourly_limit, proxy,
                                lambda: globalping.ping_with_probe(
                                    session, ep["endpoint"], anchor,
                                    proxy=proxy, slot=slot,
                                ),
                            )
                        else:
                            result = await _call_with_quota_guard(
                                hourly_limit, proxy,
                                lambda: globalping.ping_with_city(
                                    session, ep["endpoint"],
                                    city["country_iso2"], city["city"],
                                    proxy=proxy, slot=slot,
                                ),
                            )
                        await db.insert_result(
                            city_id=city_id, endpoint_id=eid,
                            measurement_id=result.measurement_id,
                            avg_ms=result.avg_ms, median_ms=result.median_ms,
                            loss_pct=result.loss_pct, probe_ip=result.probe_ip,
                            mdev_ms=result.mdev_ms, test_round=test_round,
                        )
                        await db.mark_city_probe_online(city_id)
                        await progress.tick(city_label, ep["endpoint"], result)
                    except NoProbesFoundError as e:
                        await db.set_probe_num(city_id, 0)
                        await progress.tick(city_label, ep["endpoint"], "NO_PROBES")
                        logger.warning("  %s → %s: no available probes: %s",
                                       city_label, ep["endpoint"], e)
                    except ProbeUnreachableError as e:
                        await progress.tick(city_label, ep["endpoint"], "UNREACHABLE")
                        logger.warning("  %s → %s unreachable from probe: %s",
                                       city_label, ep["endpoint"], e)
                    except Exception as e:
                        await progress.tick(city_label, ep["endpoint"], None)
                        logger.error("  %s → %s failed: %s",
                                     city_label, ep["endpoint"], e)

                async def bounded_retest_one(eid: int):
                    async with ep_sem:
                        await retest_one(eid)

                await asyncio.gather(*[
                    asyncio.create_task(bounded_retest_one(eid)) for eid in remaining_eids
                ])

    pool_size = proxy.pool_size if proxy else 1
    await asyncio.gather(*[
        asyncio.create_task(retest_city(cid, eids, slot=i % pool_size))
        for i, (cid, eids) in enumerate(stale_by_city.items())
    ])
    progress.summary()
