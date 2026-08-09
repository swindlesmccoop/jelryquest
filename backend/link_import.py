import re
from typing import Literal
from urllib.parse import unquote, urlparse

from backend.real_debrid import RealDebridClient, build_magnet

LinkKind = Literal["magnet", "rd", "ddl"]


def classify_pasted_link(raw: str) -> LinkKind:
    text = raw.strip()
    if not text:
        raise ValueError("Link is empty")
    lower = text.lower()
    if lower.startswith("magnet:"):
        return "magnet"
    if re.fullmatch(r"[a-fA-F0-9]{32,40}", text):
        return "magnet"
    if not lower.startswith("http://") and not lower.startswith("https://"):
        raise ValueError(
            "Unrecognized link. Paste a magnet, real-debrid.com URL, or direct https link."
        )
    if "real-debrid.com" in lower and "download.real-debrid.com" not in lower:
        return "rd"
    return "ddl"


def magnet_info_hash(magnet_or_hash: str) -> str:
    text = magnet_or_hash.strip()
    match = re.search(r"btih:([a-fA-F0-9]{32,40})", text, re.IGNORECASE)
    if match:
        return match.group(1).lower()
    if re.fullmatch(r"[a-fA-F0-9]{32,40}", text):
        return text.lower()
    raise ValueError("Could not parse magnet info hash")


def title_from_magnet(magnet: str) -> str | None:
    match = re.search(r"(?:^|[?&])dn=([^&]+)", magnet, re.IGNORECASE)
    if not match:
        return None
    name = unquote(match.group(1).replace("+", " "))
    return name.strip() or None


def title_from_url(url: str) -> str | None:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    name = path.split("/")[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    name = unquote(name.replace("+", " ")).strip()
    return name or None


async def resolve_pasted_link(
    raw: str,
    rd_client: RealDebridClient,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Return (direct_ddl, rd_panel_link, suggested_title, filename_hint)."""
    kind = classify_pasted_link(raw)
    text = raw.strip()

    if kind == "magnet":
        info_hash = magnet_info_hash(text)
        magnet = build_magnet(info_hash)
        resolved = await rd_client.resolve_magnet_to_rd_link(info_hash)
        magnet_title = title_from_magnet(magnet)
        filename = resolved.filename
        title = magnet_title or filename
        return None, resolved.rd_link, title, filename

    if kind == "rd":
        url_title = title_from_url(text)
        return None, text, url_title, url_title

    url_title = title_from_url(text)
    return text, None, url_title, url_title
