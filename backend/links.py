"""Classify and resolve links pasted into the "Paste link" box."""

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import unquote, urlparse

LinkKind = Literal["magnet", "rd", "ddl"]

_HASH_RE = re.compile(r"[a-fA-F0-9]{32,40}")


@dataclass
class PastedLink:
    kind: LinkKind
    direct_url: str | None = None
    rd_link: str | None = None
    info_hash: str | None = None
    title: str | None = None
    filename: str | None = None


def classify_link(raw: str) -> LinkKind:
    text = raw.strip()
    if not text:
        raise ValueError("Link is empty")
    lower = text.lower()
    if lower.startswith("magnet:") or _HASH_RE.fullmatch(text):
        return "magnet"
    if not lower.startswith(("http://", "https://")):
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
    if _HASH_RE.fullmatch(text):
        return text.lower()
    raise ValueError("Could not parse magnet info hash")


def title_from_magnet(magnet: str) -> str | None:
    match = re.search(r"(?:^|[?&])dn=([^&]+)", magnet, re.IGNORECASE)
    if not match:
        return None
    return unquote(match.group(1).replace("+", " ")).strip() or None


def title_from_url(url: str) -> str | None:
    name = urlparse(url).path.rstrip("/").split("/")[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return unquote(name.replace("+", " ")).strip() or None


def parse_pasted_link(raw: str) -> PastedLink:
    kind = classify_link(raw)
    text = raw.strip()
    if kind == "magnet":
        return PastedLink(
            kind, info_hash=magnet_info_hash(text), title=title_from_magnet(text)
        )
    name = title_from_url(text)
    if kind == "rd":
        return PastedLink(kind, rd_link=text, title=name, filename=name)
    return PastedLink(kind, direct_url=text, title=name, filename=name)

