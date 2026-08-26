"""
Background Real-Debrid CDN scan: benchmark public hosts and pick the fastest.

When REAL_DEBRID_PREFERRED_CDN is set, the scan is skipped (pinned).
Results are cached on disk with TTL and applied via runtime setters.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from copy import deepcopy
from typing import Any

import httpx

from backend.rd_urls import (
    DEFAULT_CDN_HOSTS,
    SPEEDTEST_PATHS,
    normalize_cdn_host,
    set_runtime_preferred_cdn,
)

logger = logging.getLogger(__name__)

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_cache_dir(path: str) -> str:
    path = (path or "").strip() or os.path.join(APP_DIR, "cache")
    if not os.path.isabs(path):
        path = os.path.join(APP_DIR, path)
    return os.path.realpath(path)


CACHE_DIR = _resolve_cache_dir(os.environ.get("CACHE_DIR") or os.path.join(APP_DIR, "cache"))


def _env_yes(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _split_hosts(raw: str) -> list[str]:
    parts = re.split(r"[,;\s]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


_STATE_LOCK = asyncio.Lock()
_SCAN_STARTED = False

_state: dict[str, Any] = {
    "state": "disabled",
    "target": None,
    "eta_sec": None,
    "winner": None,
    "mbps": None,
    "at": None,
    "error": None,
}


def _cache_path(name: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, name)


def _load_cache(name: str) -> dict | None:
    path = _cache_path(name)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except FileNotFoundError:
        return None
    except Exception:
        return None


def _write_cache(name: str, payload: dict) -> None:
    path = _cache_path(name)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


async def _set_state(**updates: Any) -> None:
    async with _STATE_LOCK:
        _state.update(updates)


def get_scan_status() -> dict:
    return deepcopy(_state)


def _eligible_hosts_for_scan() -> list[str]:
    raw_hosts = (os.environ.get("REAL_DEBRID_SCAN_HOSTS") or "").strip()
    if raw_hosts:
        return [normalize_cdn_host(h) for h in _split_hosts(raw_hosts)]
    return list(DEFAULT_CDN_HOSTS)


def _cache_ttl_sec() -> float:
    return _env_float("REAL_DEBRID_SCAN_CACHE_TTL_SEC", default=86400.0)


def _mbps(bytes_read: int, elapsed: float) -> float:
    if elapsed <= 0 or bytes_read <= 0:
        return 0.0
    return (bytes_read * 8) / elapsed / 1_000_000


async def _probe_host(
    client: httpx.AsyncClient,
    host: str,
    seconds: float,
    connect_to: float,
) -> dict[str, Any]:
    headers = {"User-Agent": "jelryquest-rd-cdn-speedtest/1.0"}
    last_err = ""

    for path in SPEEDTEST_PATHS:
        url = f"https://{host}{path}"
        started = time.monotonic()
        bytes_read = 0
        try:
            timeout = httpx.Timeout(seconds + 5, connect=connect_to)
            async with client.stream(
                "GET", url, headers=headers, timeout=timeout
            ) as response:
                if response.status_code != 200:
                    last_err = f"HTTP {response.status_code}"
                    continue
                async for chunk in response.aiter_bytes(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    bytes_read += len(chunk)
                    if time.monotonic() - started >= seconds:
                        break
        except httpx.HTTPError as exc:
            last_err = str(exc)[:120]
            continue

        elapsed = time.monotonic() - started
        if bytes_read > 0 and elapsed > 0:
            return {
                "host": host,
                "ok": True,
                "mbps": _mbps(bytes_read, elapsed),
                "mib_s": bytes_read / elapsed / (1024 * 1024),
                "bytes": bytes_read,
                "seconds": round(elapsed, 2),
                "path": path,
            }
        last_err = last_err or "no data"

    return {"host": host, "ok": False, "error": last_err or "unreachable"}


async def benchmark_hosts(
    client: httpx.AsyncClient,
    hosts: list[str],
    *,
    seconds: float,
    workers: int,
    connect_to: float,
) -> list[dict[str, Any]]:
    norm_hosts: list[str] = []
    seen: set[str] = set()
    for host in hosts:
        norm = normalize_cdn_host(host)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        norm_hosts.append(norm)

    if not norm_hosts:
        return []

    sem = asyncio.Semaphore(max(1, workers))

    async def run_one(host: str) -> dict[str, Any]:
        async with sem:
            return await _probe_host(client, host, seconds, connect_to)

    results = await asyncio.gather(*(run_one(host) for host in norm_hosts))
    ok = [r for r in results if r.get("ok")]
    fail = [r for r in results if not r.get("ok")]
    ok.sort(key=lambda r: r["mbps"], reverse=True)
    return [*ok, *fail]


def _apply_winner(winner_host: str) -> None:
    if not winner_host:
        return
    set_runtime_preferred_cdn(winner_host)
    logger.info("Real-Debrid CDN scan winner: %s", winner_host)


def _maybe_apply_from_cache() -> bool:
    cached = _load_cache("rd-best-cdn.json")
    if not cached:
        return False
    ttl = _cache_ttl_sec()
    if ttl <= 0:
        return False
    at = cached.get("at")
    if not isinstance(at, (int, float)):
        return False
    if time.time() - float(at) > ttl:
        return False

    host = cached.get("host")
    if not host:
        return False
    _apply_winner(host)
    _state.update(
        {
            "state": "cached",
            "target": None,
            "winner": host,
            "mbps": cached.get("mbps"),
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(at))),
            "error": None,
        }
    )
    return True


async def _run_scan(client: httpx.AsyncClient) -> None:
    try:
        hosts = [h for h in _eligible_hosts_for_scan() if h]
        if not hosts:
            raise RuntimeError("No Real-Debrid CDN hosts to scan")

        await _set_state(
            state="scanning",
            target={"type": "benchmark_cdn_hosts", "count": len(hosts)},
        )

        seconds = _env_float("REAL_DEBRID_SCAN_SECONDS", default=6.0)
        workers = max(1, _env_int("REAL_DEBRID_SCAN_WORKERS", default=8))
        connect_to = _env_float("REAL_DEBRID_SCAN_CONNECT_TIMEOUT_SEC", default=8.0)

        results = await benchmark_hosts(
            client,
            hosts,
            seconds=seconds,
            workers=workers,
            connect_to=connect_to,
        )
        ok = [r for r in results if r.get("ok")]
        if not ok:
            raise RuntimeError("Real-Debrid CDN scan returned no successful results")

        ok.sort(key=lambda r: r["mbps"], reverse=True)
        top = max(1, _env_int("REAL_DEBRID_SCAN_TOP", default=20))
        winner = ok[:top][0]
        _apply_winner(winner["host"])

        cache_payload = {
            "host": winner["host"],
            "mbps": winner.get("mbps"),
            "at": time.time(),
        }
        _write_cache("rd-best-cdn.json", cache_payload)

        await _set_state(
            state="done",
            target=None,
            winner=winner["host"],
            mbps=winner.get("mbps"),
            at=time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(cache_payload["at"])
            ),
            error=None,
        )
        logger.info(
            "Real-Debrid CDN scan complete: %s (%.1f Mbps)",
            winner["host"],
            winner.get("mbps") or 0,
        )
    except Exception as exc:
        await _set_state(state="error", error=str(exc))
        logger.exception("Real-Debrid CDN scan failed")


async def start_background_server_scan(client: httpx.AsyncClient) -> None:
    """Start background CDN scan once per process. Safe to call multiple times."""
    global _SCAN_STARTED
    if _SCAN_STARTED:
        return
    _SCAN_STARTED = True

    pinned = bool((os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip())
    rd_auto_default = not pinned
    rd_auto = _env_yes("REAL_DEBRID_AUTO_SCAN", default=rd_auto_default)

    if pinned:
        winner = (os.environ.get("REAL_DEBRID_PREFERRED_CDN") or "").strip()
        await _set_state(state="pinned", winner=winner, error=None)
        logger.info("Real-Debrid CDN pinned: %s", winner)
        return

    if not rd_auto:
        await _set_state(state="disabled", error=None)
        return

    if _maybe_apply_from_cache():
        logger.info(
            "Real-Debrid CDN from cache: %s (%.1f Mbps)",
            _state.get("winner"),
            _state.get("mbps") or 0,
        )
        return

    hosts = [h for h in _eligible_hosts_for_scan() if h]
    seconds = _env_float("REAL_DEBRID_SCAN_SECONDS", default=6.0)
    workers = max(1, _env_int("REAL_DEBRID_SCAN_WORKERS", default=8))
    eta_sec = float(seconds) * float(math.ceil(len(hosts) / float(workers)))
    await _set_state(
        state="scanning",
        target={"type": "benchmark_cdn_hosts", "count": len(hosts)},
        eta_sec=round(eta_sec, 1),
    )
    asyncio.create_task(_run_scan(client))
