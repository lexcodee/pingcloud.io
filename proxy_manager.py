"""Oxylabs DC proxy port-pool manager.

Maintains a pool of N proxy ports (one per concurrent city slot).
Each port maps to a consistent IP session on Oxylabs.
After max_uses_per_port requests through a port, or on 429 rate limit,
that slot's port rotates to the next port to get a fresh IP.

Pool size is typically set to city_concurrency so each concurrent city
gets its own port (IP), while endpoints within the same city share the port.

When pool_size=1, behavior is identical to the original single-port rotation.
"""

import asyncio
import logging
from urllib.parse import quote

logger = logging.getLogger(__name__)


class OxylabsProxyManager:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port_min: int = 8001,
        port_max: int = 63000,
        max_uses_per_port: int = 250,
        pool_size: int = 1,
    ):
        self._host = host
        self._user = quote(username, safe="")
        self._passwd = quote(password, safe="")
        self._port_min = port_min
        self._port_max = port_max
        self._max_uses = max_uses_per_port
        self._pool_size = pool_size

        # Pre-allocate ports evenly spaced across the range
        if pool_size == 1:
            self._ports = [port_min]
        else:
            step = (port_max - port_min) // pool_size
            self._ports = [port_min + i * step for i in range(pool_size)]

        self._use_counts: list[int] = [0] * pool_size
        self._locks: list[asyncio.Lock] = [asyncio.Lock() for _ in range(pool_size)]

        logger.info(
            "Proxy pool initialized: %d slots, ports %s (step %d), %d uses per port",
            pool_size,
            self._ports[:5] if pool_size > 5 else self._ports,
            (self._ports[1] - self._ports[0]) if pool_size > 1 else 0,
            max_uses_per_port,
        )
        if pool_size > 5:
            logger.info("  ... and %d more ports: %s .. %s",
                        pool_size - 5, self._ports[5], self._ports[-1])

    @property
    def pool_size(self) -> int:
        return self._pool_size

    async def get_proxy_url(self, slot: int = 0) -> str:
        """Return proxy URL for the given slot's current port and increment use count.
        Rotates to next port if use count reaches max."""
        async with self._locks[slot]:
            url = f"http://{self._user}:{self._passwd}@{self._host}:{self._ports[slot]}"
            self._use_counts[slot] += 1
            if self._use_counts[slot] >= self._max_uses:
                self._advance_port(slot)
            return url

    async def on_rate_limited(self, slot: int = 0) -> None:
        """Called on 429: rotate the given slot's port immediately."""
        async with self._locks[slot]:
            logger.info(
                "Rate limited on slot %d port %d (%d/%d uses), rotating to next port",
                slot, self._ports[slot], self._use_counts[slot], self._max_uses,
            )
            self._advance_port(slot)

    def _advance_port(self, slot: int) -> None:
        """Move the given slot to its next port and reset use count.
        Steps by pool_size to avoid colliding with adjacent slots.
        Must be called while holding the slot's lock."""
        old_port = self._ports[slot]
        self._ports[slot] += self._pool_size
        if self._ports[slot] > self._port_max:
            # Wrap back: start at port_min + slot offset
            self._ports[slot] = self._port_min + slot
            if self._ports[slot] > self._port_max:
                self._ports[slot] = self._port_min
            logger.info("Slot %d port range exhausted, wrapping back to %d",
                        slot, self._ports[slot])
        self._use_counts[slot] = 0
        logger.info("Slot %d: port %d → %d", slot, old_port, self._ports[slot])
