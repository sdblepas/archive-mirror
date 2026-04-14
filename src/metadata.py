"""
Fetches and parses Internet Archive item metadata.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import Config
from .logger import get_logger

log = get_logger(__name__)

_METADATA_URL = "https://archive.org/metadata/{identifier}"


@dataclass
class TrackInfo:
    ia_filename: str
    format: str
    title: Optional[str]
    track_number: Optional[int]
    size: Optional[int]
    md5: Optional[str]
    sha1: Optional[str]
    artist: Optional[str] = None


@dataclass
class ConcertInfo:
    identifier: str
    title: str
    artist: str
    date: str
    venue: Optional[str]
    description: Optional[str]
    flac_tracks: list[TrackInfo] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class MetadataFetcher:
    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self._cfg = config
        self._client = client

    async def fetch(self, identifier: str) -> Optional[ConcertInfo]:
        url = _METADATA_URL.format(identifier=identifier)
        raw = await self._get_json_with_retry(url, identifier)
        if raw is None:
            return None
        return _parse(identifier, raw)

    async def _get_json_with_retry(
        self, url: str, identifier: str
    ) -> Optional[dict]:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self._cfg.retry_count + 2):
            try:
                response = await self._client.get(
                    url, timeout=self._cfg.request_timeout
                )
                if response.status_code == 404:
                    log.warning("metadata.not_found", identifier=identifier)
                    return None
                if response.status_code == 429:
                    wait = int(response.headers.get("Retry-After", str(30 * attempt)))
                    log.warning(
                        "metadata.rate_limited",
                        identifier=identifier,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, OSError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt <= self._cfg.retry_count:
                    wait = min(4 * 2 ** (attempt - 1), 120)
                    log.warning(
                        "metadata.fetch_error",
                        identifier=identifier,
                        attempt=attempt,
                        error=str(exc),
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(
            f"Failed to fetch metadata for {identifier} after retries"
        ) from last_exc


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------

def _parse(identifier: str, raw: dict) -> ConcertInfo:
    meta = raw.get("metadata", {})
    files = raw.get("files", [])

    artist = _coerce_str(
        meta.get("creator") or meta.get("artist") or meta.get("uploader")
    ) or "Unknown Artist"
    title = _coerce_str(meta.get("title")) or identifier
    date = _normalise_date(_coerce_str(meta.get("date") or meta.get("year")))
    venue = _coerce_str(meta.get("venue") or meta.get("coverage"))
    description = _coerce_str(meta.get("description"))

    flac_tracks: list[TrackInfo] = []
    for f in files:
        fmt = _coerce_str(f.get("format", "")) or ""
        if fmt.lower() not in ("flac", "flac audio"):
            continue
        name = _coerce_str(f.get("name", "")) or ""
        if not name:
            continue
        flac_tracks.append(
            TrackInfo(
                ia_filename=name,
                format=fmt,
                title=_coerce_str(f.get("title")) or _stem(name),
                track_number=_parse_track_number(f.get("track")),
                size=_safe_int(f.get("size")),
                md5=_coerce_str(f.get("md5")),
                sha1=_coerce_str(f.get("sha1")),
                artist=_coerce_str(f.get("creator") or f.get("artist")),
            )
        )

    flac_tracks.sort(
        key=lambda t: (
            t.track_number if t.track_number is not None else 9999,
            t.ia_filename,
        )
    )
    _fill_track_numbers(flac_tracks)

    return ConcertInfo(
        identifier=identifier,
        title=title,
        artist=artist,
        date=date,
        venue=venue,
        description=description,
        flac_tracks=flac_tracks,
        raw=raw,
    )


def _fill_track_numbers(tracks: list[TrackInfo]) -> None:
    existing = {t.track_number for t in tracks if t.track_number is not None}
    counter = 1
    for t in tracks:
        if t.track_number is None:
            while counter in existing:
                counter += 1
            t.track_number = counter
            existing.add(counter)
            counter += 1


def _coerce_str(val: object) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _normalise_date(raw: Optional[str]) -> str:
    if not raw:
        return "unknown"
    raw = raw.split("T")[0].split(" ")[0]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw
    if re.fullmatch(r"\d{4}", raw):
        return raw
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4})", raw)
    if m:
        return m.group(1)
    return "unknown"


def _parse_track_number(raw: object) -> Optional[int]:
    s = _coerce_str(raw)
    if not s:
        return None
    s = s.split("/")[0].strip()
    try:
        return int(s)
    except ValueError:
        return None


def _safe_int(val: object) -> Optional[int]:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _stem(filename: str) -> str:
    i = filename.rfind(".")
    return filename[:i] if i > 0 else filename
