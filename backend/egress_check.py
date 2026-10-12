import json
import logging
import os
import socket
import sys
from dataclasses import dataclass

import httpx

from backend.config import Settings

logger = logging.getLogger(__name__)

DNS_PROBE_HOST = "api.ipify.org"
PUBLIC_RESOLV_CONF = "nameserver 1.1.1.1\nnameserver 8.8.8.8\noptions ndots:0\n"


def ensure_public_resolvers() -> None:
    """Write public DNS into the shared tailscale netns resolv.conf (opt-in).

    Off by default: overwriting resolv.conf breaks Docker's embedded DNS, so
    sibling service names stop resolving. Set PUBLIC_RESOLVERS=1 to enable.
    """
    if os.environ.get("PUBLIC_RESOLVERS", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    try:
        with open("/etc/resolv.conf", "w", encoding="utf-8") as f:
            f.write(PUBLIC_RESOLV_CONF)
    except OSError as exc:
        logger.debug("Could not write /etc/resolv.conf: %s", exc)


@dataclass
class EgressResult:
    ok: bool
    observed_ip: str | None
    expected_ip: str
    message: str


def parse_ip_response(text: str, url: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty egress check response")
    if "format=json" in url or stripped.startswith("{"):
        data = json.loads(stripped)
        ip = data.get("ip")
        if not ip:
            raise ValueError("JSON response missing ip field")
        return str(ip).strip()
    return stripped.split()[0]


DEFAULT_EGRESS_CHECK_URLS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


async def wait_for_dns(
    host: str = DNS_PROBE_HOST,
    *,
    retries: int = 60,
    delay: float = 2.0,
) -> None:
    """Wait until the shared tailscale namespace can resolve DNS."""
    import asyncio

    loop = asyncio.get_running_loop()
    ensure_public_resolvers()
    for attempt in range(1, retries + 1):
        try:
            await loop.run_in_executor(None, socket.gethostbyname, host)
            logger.info("DNS ready (%s)", host)
            return
        except OSError as exc:
            ensure_public_resolvers()
            if attempt == retries:
                raise RuntimeError(
                    f"DNS not ready for {host} after {retries} attempts: {exc}"
                ) from exc
            logger.warning(
                "DNS not ready (%s), attempt %s/%s: %s",
                host,
                attempt,
                retries,
                exc,
            )
            await asyncio.sleep(delay)


def egress_disabled_result() -> EgressResult:
    return EgressResult(
        ok=True, observed_ip=None, expected_ip="", message="Egress check disabled"
    )


async def check_egress(settings: Settings, client: httpx.AsyncClient | None = None) -> EgressResult:
    expected = settings.expected_egress_ip.strip()
    if not expected:
        return egress_disabled_result()
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)

    urls = [settings.egress_check_url.strip()]
    for url in DEFAULT_EGRESS_CHECK_URLS:
        if url not in urls:
            urls.append(url)

    last_error: Exception | None = None
    observed: str | None = None
    try:
        for url in urls:
            try:
                response = await client.get(url)
                response.raise_for_status()
                observed = parse_ip_response(response.text, url)
                break
            except Exception as exc:
                last_error = exc
                logger.warning("Egress probe failed via %s: %s", url, exc)
        else:
            raise last_error or RuntimeError("no egress check URLs configured")
    except Exception as exc:
        msg = f"Egress check failed: {exc}"
        if "name resolution" in str(exc).lower() or getattr(exc, "errno", None) == -3:
            msg += (
                " (DNS: ensure tailscale sidecar is connected; try TS_ACCEPT_DNS=false "
                "and explicit dns in docker-compose — see README troubleshooting)"
            )
        logger.error(msg)
        return EgressResult(ok=False, observed_ip=None, expected_ip=expected, message=msg)
    finally:
        if owns_client:
            await client.aclose()

    if observed == expected:
        msg = f"Egress OK: {observed}"
        logger.info(msg)
        return EgressResult(ok=True, observed_ip=observed, expected_ip=expected, message=msg)

    msg = f"Egress mismatch: observed={observed} expected={expected}"
    logger.error(msg)
    return EgressResult(ok=False, observed_ip=observed, expected_ip=expected, message=msg)


async def verify_egress_on_startup(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    retries: int = 18,
    delay: float = 5.0,
) -> EgressResult:
    import asyncio

    if not settings.expected_egress_ip.strip():
        logger.info("EXPECTED_EGRESS_IP not set; egress check disabled")
        return egress_disabled_result()

    last: EgressResult | None = None
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=30.0)
    try:
        for attempt in range(1, retries + 1):
            last = await check_egress(settings, client)
            if last.ok:
                return last
            if attempt < retries:
                logger.warning(
                    "Egress check attempt %s/%s failed; retrying in %ss: %s",
                    attempt,
                    retries,
                    delay,
                    last.message,
                )
                await asyncio.sleep(delay)
    finally:
        if owns_client and client is not None:
            await client.aclose()

    assert last is not None
    if settings.egress_check_strict:
        logger.error("Strict egress check failed; exiting")
        sys.exit(1)
    logger.warning("Egress check failed but EGRESS_CHECK_STRICT=false; continuing")
    return last
