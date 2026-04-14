"""
Writes Vorbis comment tags to FLAC files using mutagen.

Tags written:
  TITLE        – track title
  ARTIST       – artist name
  ALBUM        – "Artist - Date - Venue" (venue omitted if unknown)
  DATE         – concert date
  VENUE        – venue name
  TRACKNUMBER  – zero-padded (e.g. "01")
  COMMENT      – Internet Archive identifier for traceability
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from mutagen.flac import FLAC

from .logger import get_logger

log = get_logger(__name__)


def tag_flac(
    path: Path,
    *,
    title: str,
    artist: str,
    album: str,
    date: str,
    venue: Optional[str],
    track_number: int,
    total_tracks: int,
    identifier: str,
) -> None:
    """Open *path*, overwrite Vorbis comment tags, save in-place.

    Silently skips files that cannot be opened as FLAC (e.g. zero-byte
    corrupt downloads – these will be caught by checksum validation anyway).
    """
    try:
        audio = FLAC(str(path))
    except Exception as exc:
        log.warning("tagger.open_failed", path=str(path), error=str(exc))
        return

    pad = max(2, len(str(total_tracks)))

    audio["TITLE"] = [title]
    audio["ARTIST"] = [artist]
    audio["ALBUM"] = [album]
    audio["DATE"] = [date]
    audio["TRACKNUMBER"] = [str(track_number).zfill(pad)]
    audio["TRACKTOTAL"] = [str(total_tracks)]

    if venue:
        audio["VENUE"] = [venue]

    audio["COMMENT"] = [f"https://archive.org/details/{identifier}"]

    try:
        audio.save()
        log.debug("tagger.tagged", path=str(path))
    except Exception as exc:
        log.warning("tagger.save_failed", path=str(path), error=str(exc))
