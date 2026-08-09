import logging
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset(
    {
        "torrentio.strem.fun",
        "v3-cinemeta.strem.io",
        "cinemeta.strem.io",
        "stremio.cinemeta.io",
    }
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}

app = FastAPI(title="AddonBridge", docs_url=None, redoc_url=None)


def _validate_target(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "URL must be http or https")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise HTTPException(400, f"Host not allowed: {host}")
    return url.strip()


@app.get("/fetch")
async def fetch(url: str = Query(..., min_length=8)) -> Response:
    target = _validate_target(url)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=30.0),
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        ) as client:
            response = await client.get(target)
    except httpx.HTTPError as exc:
        logger.warning("Fetch failed for %s: %s", target, exc)
        raise HTTPException(502, f"Upstream fetch failed: {exc}") from exc

    media_type = response.headers.get("content-type", "application/json")
    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=media_type,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


def run() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090, log_level="info")


if __name__ == "__main__":
    run()
