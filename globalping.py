"""Globalping API wrapper: create measurements, poll results, parse stats."""

import asyncio
import logging
import math
import statistics
from dataclasses import dataclass

import aiohttp

from config import GLOBALPING_API_URL, GLOBALPING_TOKEN
from proxy_manager import OxylabsProxyManager

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5   # seconds between polls (spec says >= 500ms)
POLL_TIMEOUT  = 60    # max seconds to wait for a measurement to finish
MAX_RETRIES   = 3
RETRY_BASE_DELAY = 2  # seconds, exponential backoff base
MAX_PROXY_RETRIES = 100  # max port rotations on 429 before giving up

# Rate limiter: prevent 429 "too_many_probes" from Globalping API
API_CONCURRENCY = 150        # max simultaneous API requests
API_MIN_INTERVAL = 0.01      # min seconds between consecutive requests
_api_sem = asyncio.Semaphore(API_CONCURRENCY)
_api_lock = asyncio.Lock()
_last_request_time: float = 0.0


class NoProbesFoundError(Exception):
    """Raised when Globalping has no available probes for the requested location."""


class ProbeUnreachableError(Exception):
    """Raised when the probe cannot reach the target — non-retryable."""


class ProxyRateLimitError(Exception):
    """429 received via proxy — port has been rotated, retry immediately."""


class DirectRateLimitError(Exception):
    """429 received in direct mode — hourly quota exhausted, must wait until next hour."""


@dataclass
class PingResult:
    measurement_id: str
    avg_ms: float | None
    median_ms: float | None
    loss_pct: float | None
    mdev_ms: float | None
    probe_ip: str | None  # resolvedAddress from result (target IP seen by probe)


def _headers(proxy: OxylabsProxyManager | None = None) -> dict:
    h = {
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": "pingcloud-collector/1.0",
    }
    # Proxy mode: skip token to use anonymous quota (avoid token-based rate limits)
    if not proxy and GLOBALPING_TOKEN:
        h["Authorization"] = f"Bearer {GLOBALPING_TOKEN}"
    return h


async def _pace():
    """Enforce minimum interval between consecutive API requests."""
    global _last_request_time
    async with _api_lock:
        now = asyncio.get_event_loop().time()
        wait = API_MIN_INTERVAL - (now - _last_request_time)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_request_time = asyncio.get_event_loop().time()


# ── Create measurement ──────────────────────────────────────────────────

async def _create_measurement(
    session: aiohttp.ClientSession,
    target: str,
    locations: list[dict] | str,
    limit: int = 1,
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
) -> str:
    """POST a ping measurement, return the measurement ID.
    locations can be a list of location dicts or a measurement ID string for probe reuse."""
    async with _api_sem:
        await _pace()
        body: dict = {
            "type": "ping",
            "target": target,
            "locations": locations,
            "inProgressUpdates": False,
            "measurementOptions": {"packets": 15},
        }
        # Only set limit when locations is an array (not probe reuse)
        if isinstance(locations, list):
            body["limit"] = limit
        proxy_url = await proxy.get_proxy_url(slot) if proxy else None
        kwargs = {"json": body, "headers": _headers(proxy)}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        async with session.post(
            GLOBALPING_API_URL, **kwargs
        ) as resp:
            if resp.status == 202:
                data = await resp.json()
                return data["id"]
            text = await resp.text()
            # 422 with no_probes_found is non-retryable — probes are offline
            if resp.status == 422 and "no_probes_found" in text:
                raise NoProbesFoundError(
                    f"Globalping POST 422: {text}"
                )
            # 429 via proxy: rotate port and signal retry
            if resp.status == 429 and proxy:
                await proxy.on_rate_limited(slot)
                raise ProxyRateLimitError(
                    f"Globalping POST 429: {text}"
                )
            # 429 in direct mode: hourly quota exhausted
            if resp.status == 429 and not proxy:
                raise DirectRateLimitError(
                    f"Globalping POST 429: {text}"
                )
            # Rate-limited or other error
            raise RuntimeError(f"Globalping POST {resp.status}: {text}")


# ── Poll measurement ────────────────────────────────────────────────────

async def _poll_measurement(
    session: aiohttp.ClientSession,
    measurement_id: str,
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
) -> dict:
    """GET measurement until status != 'in-progress', return full JSON."""
    url = f"{GLOBALPING_API_URL}/{measurement_id}"
    elapsed = 0.0
    while elapsed < POLL_TIMEOUT:
        proxy_url = await proxy.get_proxy_url(slot) if proxy else None
        kwargs = {"headers": _headers(proxy)}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        async with session.get(url, **kwargs) as resp:
            # 429 during poll: rotate port and retry GET (measurement still valid)
            if resp.status == 429 and proxy:
                await proxy.on_rate_limited(slot)
                elapsed += 1  # count towards timeout to prevent infinite loop
                await asyncio.sleep(1)
                continue
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"Globalping GET {resp.status}: {text}")
            data = await resp.json()

        if data.get("status") != "in-progress":
            return data

        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

    raise TimeoutError(f"Measurement {measurement_id} did not finish within {POLL_TIMEOUT}s")


# ── Parse result ────────────────────────────────────────────────────────

def _parse_result(data: dict) -> PingResult:
    """Extract stats from a finished ping measurement response."""
    mid = data["id"]
    results = data.get("results", [])
    if not results:
        raise ValueError(f"No results in measurement {mid}")

    item = results[0]
    result = item.get("result", {})
    status = result.get("status")

    if status == "offline":
        raise ProbeUnreachableError(f"Probe offline for measurement {mid}")

    if status == "failed":
        raise ProbeUnreachableError(f"Measurement {mid} failed (probe could not reach target)")

    if status != "finished":
        raise ValueError(f"Unexpected status '{status}' for measurement {mid}")

    stats = result.get("stats", {})
    avg_ms = stats.get("avg")
    loss_pct = stats.get("loss")

    # Compute median and stddev from timings
    timings = result.get("timings", [])
    rtts = [t["rtt"] for t in timings if "rtt" in t and t["rtt"] is not None]

    if rtts:
        median_ms = statistics.median(rtts)
        mdev_ms = statistics.pstdev(rtts) if len(rtts) > 1 else 0.0
    else:
        median_ms = None
        mdev_ms = None

    # probe_ip: use resolvedAddress (the IP the probe resolved for the target)
    probe_ip = result.get("resolvedAddress")

    return PingResult(
        measurement_id=mid,
        avg_ms=avg_ms,
        median_ms=median_ms,
        loss_pct=loss_pct,
        mdev_ms=mdev_ms,
        probe_ip=probe_ip,
    )


# ── Public API ──────────────────────────────────────────────────────────

async def ping_with_city(
    session: aiohttp.ClientSession,
    target: str,
    country: str,
    city: str,
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
) -> PingResult:
    """First test for a city: locate probe by country+city, return result."""
    locations = [{"country": country, "city": city}]
    return await _run_with_retry(session, target, locations, proxy, slot)


async def ping_with_probe(
    session: aiohttp.ClientSession,
    target: str,
    anchor_measurement_id: str,
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
) -> PingResult:
    """Subsequent tests: reuse probe from a previous measurement.
    The Globalping API expects locations to be the measurement ID string directly."""
    locations = anchor_measurement_id  # type: ignore[assignment]
    return await _run_with_retry(session, target, locations, proxy, slot)


async def _run_with_retry(
    session: aiohttp.ClientSession,
    target: str,
    locations: list[dict] | str,
    proxy: OxylabsProxyManager | None = None,
    slot: int = 0,
) -> PingResult:
    """Create measurement, poll until done, parse. Retry on transient errors.
    ProxyRateLimitError (429 via proxy) triggers port rotation and immediate retry
    without counting against MAX_RETRIES."""
    last_err = None
    attempt = 0
    proxy_rotations = 0

    while attempt < MAX_RETRIES:
        try:
            mid = await _create_measurement(session, target, locations, proxy=proxy, slot=slot)
            data = await _poll_measurement(session, mid, proxy=proxy, slot=slot)
            return _parse_result(data)
        except NoProbesFoundError:
            raise  # non-retryable: probes offline, skip immediately
        except ProbeUnreachableError:
            raise  # non-retryable: target unreachable from probe, skip immediately
        except DirectRateLimitError:
            raise  # non-retryable: hourly quota exhausted, caller must wait until next hour
        except ProxyRateLimitError as e:
            proxy_rotations += 1
            if proxy_rotations >= MAX_PROXY_RETRIES:
                logger.error("Exhausted %d proxy port rotations for %s: %s",
                             MAX_PROXY_RETRIES, target, e)
                raise RuntimeError(str(e)) from e
            logger.info("Proxy 429, port rotated (%d/%d), retrying immediately",
                        proxy_rotations, MAX_PROXY_RETRIES)
            continue  # retry with new port, don't count as regular attempt
        except (aiohttp.ClientError, TimeoutError, RuntimeError, ValueError) as e:
            last_err = e
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning(
                "Attempt %d/%d failed for %s: %s — retrying in %ds",
                attempt + 1, MAX_RETRIES, target, e, delay,
            )
            await asyncio.sleep(delay)
            attempt += 1

    logger.error("All %d retries exhausted for target=%s locations=%s: %s",
                 MAX_RETRIES, target, locations, last_err)
    raise last_err  # type: ignore[misc]
