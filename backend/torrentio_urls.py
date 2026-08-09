"""Parse Torrentio stream URLs (resolve/debrid paths)."""

import re
from urllib.parse import unquote, urlparse

_HASH_RE = re.compile(r"^[a-fA-F0-9]{32,40}$")


def is_torrentio_resolve_url(url: str | None) -> bool:
    raw = (url or "").strip().lower()
    return "torrentio.strem.fun/resolve/" in raw


def parse_torrentio_stream_url(url: str) -> dict[str, str | int | None]:
    """Extract info_hash, file_idx, and filename from a Torrentio resolve URL."""
    raw = (url or "").strip()
    if not is_torrentio_resolve_url(raw):
        return {}

    parts = [p for p in urlparse(raw).path.split("/") if p]
    try:
        resolve_idx = parts.index("resolve")
    except ValueError:
        return {}

    after = parts[resolve_idx + 1:]
    for i, part in enumerate(after):
        if not _HASH_RE.match(part):
            continue
        info_hash = part.lower()
        rest = after[i + 1:]
        file_idx: int | None = None
        filename: str | None = None

        if rest:
            if rest[0].lower() in ("null", "none", "-"):
                if len(rest) > 1 and rest[1].isdigit():
                    file_idx = int(rest[1])
                    if len(rest) > 2:
                        filename = unquote(rest[2])
            elif rest[0].isdigit():
                file_idx = int(rest[0])
                if len(rest) > 1:
                    filename = unquote(rest[1])
            elif len(rest) > 2 and rest[1].lower() in ("null", "none", "-"):
                if rest[2].isdigit():
                    file_idx = int(rest[2])
                if len(rest) > 3:
                    filename = unquote(rest[3])

        return {
            "info_hash": info_hash,
            "file_idx": file_idx,
            "filename": filename,
        }

    return {}
