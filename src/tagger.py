"""
Writes Vorbis comment tags to FLAC files using mutagen.
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
) -> bool:
    """Write Vorbis comment tags to a FLAC file.

    Returns True on success, False if the file could not be tagged
    (e.g. corrupt, zero-byte, or not a valid FLAC).
    """
    if not path.exists() or path.stat().st_size == 0:
        log.warning("tagger.empty_or_missing", path=str(path))
        return False

    try:
        audio = FLAC(str(path))
    except Exception as exc:
        log.warning("tagger.open_failed", path=str(path), error=str(exc))
        return False

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
        return True
    except Exception as exc:
        log.warning("tagger.save_failed", path=str(path), error=str(exc))
        return False
