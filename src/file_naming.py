"""
Filename and folder-name generation.

All output must be safe for Linux filesystems and human-readable.

Folder naming:  Artist - YYYY-MM-DD
Track naming:   01 - Track Title - Artist.flac

If two tracks in the same concert would produce the same filename after
sanitisation, a numeric suffix is appended to disambiguate.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional


# Characters forbidden on Linux ext4 / most POSIX filesystems
_FORBIDDEN = re.compile(r'[/\x00]')
# Characters that are annoying in shells or filenames
_ANNOYING = re.compile(r'[\\:*?"<>|]')
# Collapse runs of whitespace
_SPACES = re.compile(r'\s+')
# Trailing dots/spaces (Windows compat + aesthetic)
_TRAILING = re.compile(r'[\s.]+$')
# Leading dots/spaces
_LEADING = re.compile(r'^[\s.]+')


def sanitize(s: str, *, max_len: int = 200) -> str:
    """Remove or replace characters that are unsafe in filenames.

    Applies Unicode NFKC normalisation, strips control chars, replaces
    forbidden chars with underscores, and trims leading/trailing noise.
    """
    # Normalise Unicode (e.g. full-width chars → ASCII equivalents)
    s = unicodedata.normalize("NFKC", s)
    # Remove ASCII control characters
    s = "".join(c for c in s if unicodedata.category(c) != "Cc" or c == "\t")
    # Replace genuinely forbidden chars
    s = _FORBIDDEN.sub("_", s)
    # Replace annoying-but-not-technically-forbidden chars with a dash
    s = _ANNOYING.sub("-", s)
    # Collapse whitespace
    s = _SPACES.sub(" ", s)
    # Strip leading/trailing noise
    s = _LEADING.sub("", s)
    s = _TRAILING.sub("", s)
    # Hard cap on length (leave room for extension + track prefix)
    s = s[:max_len]
    # After all that we might end up empty
    return s or "untitled"


def make_folder_name(artist: str, date: str) -> str:
    """Return ``Artist - YYYY-MM-DD`` (or whatever date precision we have)."""
    a = sanitize(artist, max_len=120)
    d = sanitize(date, max_len=40)
    return f"{a} - {d}"


def make_track_filename(
    track_number: int,
    title: str,
    artist: str,
    *,
    total_tracks: int = 99,
) -> str:
    """Return ``01 - Title - Artist.flac`` with zero-padded track number.

    The pad width is derived from *total_tracks* (minimum 2 digits).
    """
    pad = max(2, len(str(total_tracks)))
    num = str(track_number).zfill(pad)
    safe_title = sanitize(title, max_len=120)
    safe_artist = sanitize(artist, max_len=80)
    return f"{num} - {safe_title} - {safe_artist}.flac"


def deduplicate_filenames(names: list[str]) -> list[str]:
    """Append ``(2)``, ``(3)`` … to duplicate filenames.

    Operates on the stem (without extension) and reattaches the extension.
    """
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        dot = name.rfind(".")
        stem, ext = (name[:dot], name[dot:]) if dot > 0 else (name, "")
        key = stem.lower()
        if key not in seen:
            seen[key] = 1
            result.append(name)
        else:
            seen[key] += 1
            result.append(f"{stem} ({seen[key]}){ext}")
    return result


def build_album_tag(artist: str, date: str, venue: Optional[str]) -> str:
    """Compose the FLAC ALBUM tag: ``Artist - Date - Venue`` (venue optional)."""
    parts = [artist, date]
    if venue:
        parts.append(venue)
    return " - ".join(parts)
