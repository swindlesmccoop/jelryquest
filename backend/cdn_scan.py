"""Background benchmark of Real-Debrid CDN nodes; the fastest becomes preferred.

Skipped when a CDN host is pinned in config. The winner is cached on disk.
"""

import asyncio
import json
import logging
import math
import os
import time
from copy import deepcopy
from typing import Any

import httpx

from backend.config import Settings
from backend.rd import CdnPreference, normalize_cdn_host, split_hosts

logger = logging.getLogger(__name__)

# Community-maintained list of public download nodes; not exhaustive.
DEFAULT_CDN_HOSTS = [
    *[
        f"{n}.download.real-debrid.com"
        for n in (*range(20, 24), *range(30, 35), *range(40, 46), *range(50, 70))
    ],
    *[
        f"{name}.download.real-debrid.com"
        for name in (
            "rbx", "den1", "sea1", "nyk1", "chi1", "lax1", "mia1", "dal1", "qro1",
            "sao1", "scl1", "lon1", "hkg1", "sgp1", "tyo1", "mum1", "tlv1", "jnb1",
        )
    ],
    "45.download.real-debrid.cloud",
]
SPEEDTEST_PATHS = ("/speedtest/test.rar", "/speedtest/testDefault.rar")
CACHE_FILE = "rd-best-cdn.json"


def _mbps(num_bytes: int, elapsed: float) -> float:
    if elapsed <= 0 or num_bytes <= 0:
        return 0.0
    return num_bytes * 8 / elapsed / 1_000_000


async def probe_host(
    client: httpx.AsyncClient, host: str, seconds: float, connect_timeout: float
) -> dict[str, Any]:
    headers = {"User-Agent": "jelryquest-rd-cdn-speedtest/1.0"}
    last_err = ""
    for path in SPEEDTEST_PATHS:
        started = time.monotonic()
        received = 0
        try:
            timeout = httpx.Timeout(seconds + 5, connect=connect_timeout)
            async with client.stream(
                "GET", f"https://{host}{path}", headers=headers, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    last_err = f"HTTP {response.status_code}"
                    continue
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    received += len(chunk)
                    if time.monotonic() - started >= seconds:
                        break
        except httpx.HTTPError as exc:
            last_err = str(exc)[:120]
            continue
        elapsed = time.monotonic() - started
        if received > 0 and elapsed > 0:
            return {"host": host, "ok": True, "mbps": _mbps(received, elapsed)}
        last_err = last_err or "no data"
    return {"host": host, "ok": False, "error": last_err or "unreachable"}


async def benchmark(
    client: httpx.AsyncClient,
    hosts: list[str],
    *,
    seconds: float,
    workers: int,
    connect_timeout: float,
) -> list[dict[str, Any]]:
    sem = asyncio.Semaphore(max(1, workers))

    async def one(host: str) -> dict[str, Any]:
        async with sem:
            return await probe_host(client, host, seconds, connect_timeout)

    results = await asyncio.gather(*(one(h) for h in dict.fromkeys(hosts)))
    ok = sorted((r for r in results if r["ok"]), key=lambda r: r["mbps"], reverse=True)
    return [*ok, *(r for r in results if not r["ok"])]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class CdnScanner:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, cdn: CdnPreference):
        self.settings = settings
        self.client = client
        self.cdn = cdn
        self._task: asyncio.Task | None = None
        self._status: dict[str, Any] = {
            "state": "disabled",
            "target": None,
            "eta_sec": None,
            "winner": None,
            "mbps": None,
            "at": None,
            "error": None,
        }

    @property
    def cache_path(self) -> str:
        return os.path.join(self.settings.cache_dir, CACHE_FILE)

    def status(self) -> dict[str, Any]:
        return deepcopy(self._status)

    def _hosts(self) -> list[str]:
        return split_hosts(self.settings.real_debrid_scan_hosts) or list(DEFAULT_CDN_HOSTS)

    def _load_cache(self) -> dict[str, Any] | None:
        ttl = self.settings.real_debrid_scan_cache_ttl_sec
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                data = json.load(f)
            at = float(data["at"])
            host = normalize_cdn_host(data["host"])
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if ttl <= 0 or not host or time.time() - at > ttl:
            return None
        return {**data, "host": host, "at": at}

    def _write_cache(self, host: str, mbps: float | None, at: float) -> None:
        try:
            os.makedirs(self.settings.cache_dir, exist_ok=True)
            tmp = f"{self.cache_path}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"host": host, "mbps": mbps, "at": at}, f)
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            logger.warning("Could not write CDN scan cache: %s", exc)

    def start(self) -> None:
        """Pin, load from cache, or kick off a scan. Call once at startup."""
        s = self.settings
        if self.cdn.pinned:
            self._status.update(state="pinned", winner=self.cdn.pinned[0])
            logger.info("Real-Debrid CDN pinned: %s", self.cdn.pinned[0])
            return
        if not s.real_debrid_auto_scan:
            return

        cached = self._load_cache()
        if cached:
            self.cdn.runtime = cached["host"]
            self._status.update(
                state="cached",
                winner=cached["host"],
                mbps=cached.get("mbps"),
                at=_iso(cached["at"]),
            )
            logger.info("Real-Debrid CDN from cache: %s", cached["host"])
            return

        hosts = self._hosts()
        eta = s.real_debrid_scan_seconds * math.ceil(
            len(hosts) / max(1, s.real_debrid_scan_workers)
        )
        self._status.update(
            state="scanning",
            target={"type": "benchmark_cdn_hosts", "count": len(hosts)},
            eta_sec=round(eta, 1),
        )
        self._task = asyncio.create_task(self._run(hosts))

    async def _run(self, hosts: list[str]) -> None:
        s = self.settings
        try:
            results = await benchmark(
                self.client,
                hosts,
                seconds=s.real_debrid_scan_seconds,
                workers=s.real_debrid_scan_workers,
                connect_timeout=s.real_debrid_scan_connect_timeout_sec,
            )
            if not results or not results[0]["ok"]:
                raise RuntimeError("Real-Debrid CDN scan returned no successful results")
            winner = results[0]
            now = time.time()
            self.cdn.runtime = winner["host"]
            self._write_cache(winner["host"], winner["mbps"], now)
            self._status.update(
                state="done",
                target=None,
                eta_sec=None,
                winner=winner["host"],
                mbps=winner["mbps"],
                at=_iso(now),
                error=None,
            )
            logger.info(
                "Real-Debrid CDN scan winner: %s (%.1f Mbps)", winner["host"], winner["mbps"]
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._status.update(state="error", target=None, eta_sec=None, error=str(exc))
            logger.exception("Real-Debrid CDN scan failed")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
