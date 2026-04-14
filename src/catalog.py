"""
Catalog export — generates a JSON + CSV snapshot of the mirrored library.

Outputs:
  {state_dir}/catalog.json   – full machine-readable catalog
  {state_dir}/catalog.csv    – flat CSV for spreadsheet / import tools

Called at the end of each sync run.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles

from .config import Config
from .database import Database
from .logger import get_logger

log = get_logger(__name__)


async def export_catalog(config: Config, db: Database) -> dict[str, int]:
    """Build and write catalog files. Returns summary counts."""
    log.info("catalog.export_start")

    # Fetch all complete items and their tracks
    items, total = await db.get_items_paginated(
        status="complete", per_page=100_000
    )

    rows: list[dict[str, Any]] = []
    csv_rows: list[dict[str, str]] = []

    for item in items:
        tracks = await db.get_tracks_for_item(item["identifier"])
        track_list = [
            {
                "ia_filename": t["ia_filename"],
                "local_filename": t["local_filename"],
                "local_path": t["local_path"],
                "title": t["title"],
                "track_number": t["track_number"],
                "md5": t["md5"],
            }
            for t in tracks
            if t["status"] == "complete"
        ]

        rows.append(
            {
                "identifier": item["identifier"],
                "collection": item["collection"],
                "artist": item["artist"],
                "date": item["date"],
                "venue": item["venue"],
                "title": item["title"],
                "folder": item["folder_name"],
                "track_count": len(track_list),
                "tracks": track_list,
            }
        )

        # One CSV row per track
        for t in track_list:
            csv_rows.append(
                {
                    "identifier": item["identifier"],
                    "collection": item["collection"] or "",
                    "artist": item["artist"] or "",
                    "date": item["date"] or "",
                    "venue": item["venue"] or "",
                    "folder": item["folder_name"] or "",
                    "track_number": str(t["track_number"] or ""),
                    "title": t["title"] or "",
                    "local_filename": t["local_filename"] or "",
                    "local_path": t["local_path"] or "",
                    "md5": t["md5"] or "",
                }
            )

    catalog = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "collections": config.collections,
        "total_items": len(rows),
        "total_tracks": len(csv_rows),
        "items": rows,
    }

    # ── Write JSON ────────────────────────────────────────────────────────
    async with aiofiles.open(config.catalog_json_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(catalog, indent=2, ensure_ascii=False))

    # ── Write CSV ─────────────────────────────────────────────────────────
    fieldnames = [
        "identifier", "collection", "artist", "date", "venue",
        "folder", "track_number", "title", "local_filename",
        "local_path", "md5",
    ]
    csv_text_rows = [",".join(fieldnames)]
    for row in csv_rows:
        csv_text_rows.append(
            ",".join(
                f'"{row[f].replace(chr(34), chr(34)+chr(34))}"'
                for f in fieldnames
            )
        )
    async with aiofiles.open(config.catalog_csv_path, "w", encoding="utf-8") as f:
        await f.write("\n".join(csv_text_rows) + "\n")

    log.info(
        "catalog.export_done",
        items=len(rows),
        tracks=len(csv_rows),
        json_path=str(config.catalog_json_path),
        csv_path=str(config.catalog_csv_path),
    )

    return {"items": len(rows), "tracks": len(csv_rows)}
