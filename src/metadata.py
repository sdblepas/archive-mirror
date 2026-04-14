"""
Fetches and parses Internet Archive item metadata.

The IA metadata API returns a JSON document with two top-level keys:
  metadata  – dict of item-level fields (title, creator, date, venue, …)
  files     – list of file objects (name, format, size, md5, sha1, title, track, …)

This module normalises the raw IA data into clean Python dataclasses so the
rest of the pipeline does not have to handle IA-specific quirks.
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


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TrackInfo:
    ia_filename: str
    format: str           # "Flac", "VBR MP3", …
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
    date: str             # "YYYY-MM-DD", "YYYY-MM" or "YYYY" if partial
    venue: Optional[str]
    description: Optional[str]
    flac_tracks: list[TrackInfo] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MetadataFetcher
# ---------------------------------------------------------------------------

class MetadataFetcher:
    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        self._cfg = config
        self._client = client

    async def fetch(self, identifier: str) -> Optional[ConcertInfo]:
        """Fetch IA metadata for *identifier* and return a ConcertInfo.

        Returns None if the item cannot be found (404).
        Raises on other network/server errors after retries.
        """
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
                    wait = 30 * attempt
                    log.warning(
                        "metadata.rate_limited",
                        identifier=identifier,
                        wait_seconds=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, Exception) as exc:
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
# Pure parsing logic (no I/O)
# ---------------------------------------------------------------------------

def _parse(identifier: str, raw: dict) -> ConcertInfo:
    meta = raw.get("metadata", {})
    files = raw.get("files", [])

    artist = _coerce_str(
        meta.get("creator") or meta.get("artist") or meta.get("uploader") or "Unknown Artist"
    )
    title = _coerce_str(
        meta.get("title") or identifier
    )
    date = _normalise_date(
        _coerce_str(meta.get("date") or meta.get("year") or "")
    )
    venue = _coerce_str(meta.get("venue") or meta.get("coverage") or None)
    description = _coerce_str(meta.get("description") or None)

    flac_tracks: list[TrackInfo] = []
    for f in files:
        fmt = _coerce_str(f.get("format", ""))
        # Internet Archive normalises FLAC format as "Flac"
        if fmt.lower() not in ("flac", "flac audio"):
            continue
        name = _coerce_str(f.get("name", ""))
        if not name:
            continue

        track_title = _coerce_str(
            f.get("title") or _stem(name)
        )
        track_num = _parse_track_number(f.get("track"))
        size = _safe_int(f.get("size"))
        md5 = _coerce_str(f.get("md5") or None)
        sha1 = _coerce_str(f.get("sha1") or None)
        # Some items have per-track creator overrides
        track_artist = _coerce_str(f.get("creator") or f.get("artist") or None)

        flac_tracks.append(
            TrackInfo(
                ia_filename=name,
                format=fmt,
                title=track_title,
                track_number=track_num,
                size=size,
                md5=md5,
                sha1=sha1,
                artist=track_artist,
            )
        )

    # Sort by track number where available, then filename
    flac_tracks.sort(
        key=lambda t: (t.track_number if t.track_number is not None else 9999, t.ia_filename)
    )

    # Assign sequential numbers to tracks that are missing them
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
    """Ensure every track has a number.  Gaps or collisions → sequential fill."""
    missing = [t for t in tracks if t.track_number is None]
    if not missing:
        return
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
    """IA metadata values can be lists – take the first element."""
    if val is None:
        return None
    if isinstance(val, list):
        val = val[0] if val else None
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


def _normalise_date(raw: Optional[str]) -> str:
    """Return YYYY-MM-DD, YYYY-MM, or YYYY.  Falls back to 'unknown'."""
    if not raw:
        return "unknown"
    # Strip time component if present
    raw = raw.split("T")[0].split(" ")[0]
    # Match YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return raw
    # Match YYYY-MM
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return raw
    # Match YYYY
    if re.fullmatch(r"\d{4}", raw):
        return raw
    # Try to extract a date-like substring
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4})", raw)
    if m:
        return m.group(1)
    return "unknown"


def _parse_track_number(raw: object) -> Optional[int]:
    """Parse "1", "01", "1/12", or None."""
    s = _coerce_str(raw)
    if not s:
        return None
    # "1/12" → 1
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
    """Return filename without extension."""
    i = filename.rfind(".")
    return filename[:i] if i > 0 else filename
