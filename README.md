# archive-mirror

A production-grade background service that continuously mirrors an Internet
Archive collection — by default the **Aadam Jacobs** concert archive — to local
disk in FLAC format with full metadata tagging.

---

## Quick start

```bash
cp .env.example .env
# Edit .env if you want to change concurrency, sync interval, etc.

docker compose up -d
docker compose logs -f
```

That's it. The service will:
1. Discover every concert in the collection.
2. Download all FLAC tracks for each concert.
3. Write FLAC Vorbis comment tags.
4. Write a `checksums.md5` manifest per concert folder.
5. Sleep until the next sync interval, then repeat.

---

## Example folder tree

```
/data/music/
├── Aadam Jacobs - 2005-06-15/
│   ├── 01 - Opening Song - Aadam Jacobs.flac
│   ├── 02 - Second Set Opener - Aadam Jacobs.flac
│   ├── 03 - Closing Number - Aadam Jacobs.flac
│   └── checksums.md5
├── Aadam Jacobs - 2006-07-20/
│   ├── 01 - Show Title - Aadam Jacobs.flac
│   └── checksums.md5
└── Aadam Jacobs - unknown/
    └── 01 - Untitled - Aadam Jacobs.flac
```

---

## Architecture

```
src/
├── config.py       Environment variable config dataclass
├── logger.py       structlog → JSON to stdout
├── database.py     aiosqlite wrapper (items / tracks / sync_runs tables)
├── discovery.py    Internet Archive scrape API with cursor pagination
├── metadata.py     IA metadata JSON parser → ConcertInfo / TrackInfo
├── file_naming.py  Filename sanitisation, folder naming, deduplication
├── downloader.py   Async httpx downloader: resume, checksum, retry
├── tagger.py       mutagen FLAC Vorbis comment writer
├── health.py       Threaded HTTP /health + /metrics endpoint
├── sync.py         SyncManager orchestrator
├── scheduler.py    Periodic asyncio loop
└── main.py         Entry point, signal handling
```

---

## State and persistence

Two Docker volumes are mounted:

| Volume  | Mount          | Contents                              |
|---------|----------------|---------------------------------------|
| `music` | `/data/music`  | Downloaded FLAC files, organised by concert folder |
| `state` | `/data/state`  | `mirror.db` (SQLite), `.health` file  |

### SQLite schema

**`items`** – one row per Internet Archive item (concert)

| Column        | Description                                      |
|---------------|--------------------------------------------------|
| `identifier`  | IA item identifier (primary key)                 |
| `status`      | `pending` / `no_flac` / `downloading` / `complete` / `failed` |
| `has_flac`    | Whether the item has any FLAC files              |
| `folder_name` | Folder name on disk                              |
| `retry_count` | How many times the item has failed and been retried |
| `raw_metadata`| Full JSON blob from the IA metadata API          |

**`tracks`** – one row per FLAC file within an item

| Column           | Description                                   |
|------------------|-----------------------------------------------|
| `item_identifier`| FK → items                                    |
| `ia_filename`    | Remote filename on Internet Archive            |
| `local_filename` | Sanitised filename on disk                    |
| `status`         | `pending` / `complete` / `failed` / `skipped` |
| `md5` / `sha1`   | Checksums from IA metadata                    |

**`sync_runs`** – history of sync operations with summary statistics

---

## How incremental sync works

On every sync cycle:

1. **Discovery** pages through the IA scrape API and calls `upsert_item` for
   each identifier found.  If the identifier already exists in the DB, the
   upsert is a no-op.  New identifiers are inserted with `status = pending`.

2. **Work list** is built from rows where `status IN ('pending')` PLUS rows
   where `status = 'failed' AND retry_count < RETRY_COUNT`.

3. Items already `complete` or `no_flac` are **never revisited** — they are
   simply excluded from the work list at query time.

4. After a successful item sync, `status` is set to `complete`.  Subsequent
   syncs skip it entirely regardless of whether new concerts have been
   uploaded to the collection.

---

## How duplicate downloads are avoided

At three layers:

1. **DB gate** — `status = 'complete'` items are excluded from the work list
   before any HTTP requests are made.

2. **File-level gate** — `downloader.py` checks whether the destination file
   already exists with the correct size (and checksum if available) before
   opening any network connection.  If valid, it returns `SKIPPED_EXISTING`
   immediately.

3. **Partial file resume** — If a `.part` file exists (from a previous
   interrupted download), the downloader issues an HTTP `Range` request to
   resume from the byte offset already written.  After completion, checksums
   are verified against the IA metadata before the `.part` file is renamed
   to its final name.

---

## Configuration reference

All settings are read from environment variables.

| Variable           | Default       | Description                                        |
|--------------------|---------------|----------------------------------------------------|
| `COLLECTION`       | `aadamjacobs` | Internet Archive collection identifier             |
| `OUTPUT_DIR`       | `/data/music` | Root directory for downloaded files                |
| `STATE_DIR`        | `/data/state` | Directory for SQLite DB and health file            |
| `SYNC_INTERVAL`    | `3600`        | Seconds between syncs (0 = run once)               |
| `CONCURRENCY`      | `3`           | Max simultaneous track downloads                   |
| `RATE_LIMIT_DELAY` | `1.0`         | Min seconds between requests per worker            |
| `REQUEST_TIMEOUT`  | `120`         | Per-request HTTP timeout in seconds                |
| `RETRY_COUNT`      | `5`           | Max retries per download or metadata fetch         |
| `LOG_LEVEL`        | `INFO`        | `DEBUG` / `INFO` / `WARNING` / `ERROR`             |
| `DRY_RUN`          | `false`       | List what would be downloaded without writing      |
| `CHECKSUM_MANIFEST`| `true`        | Write `checksums.md5` to each concert folder       |
| `WEBHOOK_URL`      | *(empty)*     | Optional URL to POST a JSON summary after each sync|
| `HEALTH_PORT`      | `8080`        | Port for the `/health` and `/metrics` endpoints    |

---

## Health check

```bash
curl http://localhost:8080/health
# {"status": "ok", "sync_status": "sleeping", "next_sync_in_seconds": 3542}

curl http://localhost:8080/metrics
# {"last_sync": {"items_discovered": 412, "tracks_downloaded": 3890, ...}}
```

Docker's own `HEALTHCHECK` instruction polls `/health` every 30 seconds.

---

## Observability

All log lines are JSON, emitted to stdout.  Key event names:

| Event                   | When                                      |
|-------------------------|-------------------------------------------|
| `sync.start`            | Beginning of a sync cycle                 |
| `sync.discovery_complete` | All items discovered                    |
| `sync.no_flac`          | Item has no FLAC files — skipped          |
| `sync.processing`       | Starting downloads for an item            |
| `sync.item_complete`    | All tracks for an item downloaded         |
| `sync.item_partial`     | Some tracks failed — will retry           |
| `download.skip_existing`| File already present and valid — skipped  |
| `download.retry`        | Transient failure — retrying              |
| `download.failed`       | All retries exhausted                     |
| `sync.complete`         | Sync cycle finished with statistics       |
| `scheduler.sleeping`    | Waiting until next sync                   |

---

## Mounting host directories instead of named volumes

Edit `docker-compose.yml` and uncomment the `driver_opts` sections:

```yaml
volumes:
  music:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/nas/archive/music
  state:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: /mnt/nas/archive/state
```

---

## Running without Docker

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export OUTPUT_DIR=./music
export STATE_DIR=./state
export SYNC_INTERVAL=0       # run once
export DRY_RUN=true          # preview only

python -m src.main
```

---

## Edge cases handled

| Scenario                           | Handling                                                  |
|------------------------------------|-----------------------------------------------------------|
| Missing FLAC files in item         | Item marked `no_flac`; clearly logged; never retried      |
| Partial / interrupted download     | `.part` file resumed via `Range` header on next run       |
| Checksum mismatch                  | File deleted, re-downloaded up to `RETRY_COUNT` times     |
| Missing date in metadata           | Folder named `Artist - unknown`                          |
| Missing venue                      | Album tag omits venue; no crash                           |
| Duplicate track titles             | `(2)`, `(3)` suffixes appended by `deduplicate_filenames` |
| Track numbers absent or "1/12"     | `_fill_track_numbers` assigns sequential numbers          |
| Multiple artist names (list)       | `_coerce_str` takes first element                         |
| 429 rate limiting                  | Respects `Retry-After` header; backs off exponentially    |
| Container restart mid-sync         | `status = 'downloading'` items reset to `pending` on next startup (retry path) |
| Very large collections (>10 000)   | Scrape API cursor pagination handles unlimited results    |
