# 🎙️ archive-mirror

[![Build & Publish Docker](https://github.com/sdblepas/archive-mirror/actions/workflows/build.yml/badge.svg)](https://github.com/sdblepas/archive-mirror/actions/workflows/build.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/sdblepas/archive-mirror)](https://hub.docker.com/r/sdblepas/archive-mirror)
[![Docker Image Size](https://img.shields.io/docker/image-size/sdblepas/archive-mirror/latest)](https://hub.docker.com/r/sdblepas/archive-mirror)
[![Docker Version](https://img.shields.io/docker/v/sdblepas/archive-mirror?sort=semver&label=version)](https://hub.docker.com/r/sdblepas/archive-mirror/tags)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/sdblepas/archive-mirror)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/sdblepas/archive-mirror)](https://github.com/sdblepas/archive-mirror/commits/main)

> A self-hosted Docker service that automatically mirrors the **Aadam Jacobs Collection** from the Internet Archive — downloading every concert in FLAC, tagging the files, and keeping your library in sync.

---

## 🎸 About the Aadam Jacobs Collection

Aadam Jacobs is Chicago's legendary "Taping Guy." For over two decades he attended a dozen or more gigs a month, setting up microphones at iconic venues like **Lounge Ax**, **The Metro**, **the Double Door**, and **Smart Bar** — capturing what no one else was preserving. His tapes document early performances by bands who went on to define alternative rock: **Nirvana, Sonic Youth, The Flaming Lips**, and thousands more.

His archive spans roughly **10,000 tapes** — approximately **30,000 separate performances** — recorded from the early 1980s through the 2000s, first on cassette, then DAT, then digital.

> *"My passion is really to document something that's otherwise not being documented. It's more a desire to collect and archive this stuff."*
> — Aadam Jacobs, Glorious Noise, 2004

In partnership with the **[Live Music Archive at the Internet Archive](https://archive.org/details/aadamjacobs)**, Jacobs' collection is being digitized and shared with the world by a volunteer team. The project began in the fall of 2024. His work was spotlighted in the 2023 documentary **Melomaniac** (dir. Katlin Schneider) and featured on Chicago Public Radio (WBEZ).

This tool exists to make that archive permanently available on your own hardware.

---

## ✨ Features

- 🔍 **Full collection discovery** — cursor-paginated scrape API, handles 30 000+ items
- ⬇️ **FLAC-only downloads** — skips concerts with no lossless audio
- ♻️ **Incremental sync** — only fetches what's new or changed, safe to restart anytime
- 📂 **Clean folder structure** — `Artist - YYYY-MM-DD / 01 - Title - Artist.flac`
- 🏷️ **Auto-tagging** — writes `TITLE`, `ARTIST`, `ALBUM`, `DATE`, `VENUE`, `TRACKNUMBER`
- ✅ **Checksum validation** — MD5/SHA-1 verified against Internet Archive metadata
- ⏸️ **Resume support** — interrupted downloads continue from where they stopped
- 🔁 **Retry logic** — exponential back-off with configurable retry count
- 🩺 **Health endpoint** — `GET /health` and `GET /metrics` on port `6547`
- 🧾 **SQLite state** — full history of every item and track, survives restarts
- 🐳 **Docker-native** — single `docker compose up -d` to run

---

## 🚀 Installation

### Prerequisites

- Docker ≥ 24
- Docker Compose v2
- ~500 GB+ free disk space (the full archive is large)

### 1 — Create the data directories

On your host (adjust the path to match your setup):

```bash
mkdir -p /volume1/Docker/archive-mirror/music
mkdir -p /volume1/Docker/archive-mirror/state
```

### 2 — Create `docker-compose.yml`

```yaml
version: "3.9"

services:
  archive-mirror:
    image: sdblepas/archive-mirror:latest
    container_name: archive-mirror
    restart: unless-stopped

    volumes:
      - /volume1/Docker/archive-mirror/music:/data/music
      - /volume1/Docker/archive-mirror/state:/data/state

    environment:
      COLLECTION: aadamjacobs
      OUTPUT_DIR: /data/music
      STATE_DIR: /data/state
      SYNC_INTERVAL: "3600"      # seconds between syncs (0 = run once)
      CONCURRENCY: "3"           # parallel downloads
      RATE_LIMIT_DELAY: "1.0"    # seconds between requests per worker
      REQUEST_TIMEOUT: "120"
      RETRY_COUNT: "5"
      LOG_LEVEL: INFO
      DRY_RUN: "false"
      CHECKSUM_MANIFEST: "true"
      WEBHOOK_URL: ""
      HEALTH_PORT: "6547"

    ports:
      - "6547:6547"

    healthcheck:
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; r=urllib.request.urlopen('http://localhost:6547/health',timeout=5); sys.exit(0 if r.status==200 else 1)"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 20s

    logging:
      driver: json-file
      options:
        max-size: "50m"
        max-file: "5"
```

### 3 — Start the service

```bash
docker compose up -d
```

### 4 — Follow the logs

```bash
docker compose logs -f
```

You will see structured JSON logs like:

```json
{"event": "sync.start", "collection": "aadamjacobs", "dry_run": false}
{"event": "sync.discovery_complete", "discovered": 412, "new": 412}
{"event": "sync.processing", "identifier": "aj1990-11-09", "artist": "Nirvana", "tracks": 14}
{"event": "download.complete", "filename": "01 - Blew - Nirvana.flac", "bytes": 42817234}
{"event": "sync.item_complete", "folder": "Nirvana - 1990-11-09", "tracks": 14}
{"event": "sync.complete", "tracks_downloaded": 3890, "failures": 0}
```

### 5 — Check health

```bash
curl http://localhost:6547/health
# {"status": "ok", "sync_status": "sleeping", "next_sync_in_seconds": 3542}

curl http://localhost:6547/metrics
# {"last_sync": {"items_discovered": 412, "tracks_downloaded": 3890, "failures": 0}}
```

---

## 📁 Output structure

```
/data/music/
├── Nirvana - 1990-11-09/
│   ├── 01 - Blew - Nirvana.flac
│   ├── 02 - School - Nirvana.flac
│   ├── 03 - Love Buzz - Nirvana.flac
│   └── checksums.md5
├── Sonic Youth - 1991-05-03/
│   ├── 01 - Dirty Boots - Sonic Youth.flac
│   └── checksums.md5
└── The Flaming Lips - 1992-08-14/
    ├── 01 - She Don't Use Jelly - The Flaming Lips.flac
    └── checksums.md5
```

---

## ⚙️ Configuration reference

| Variable | Default | Description |
|---|---|---|
| `COLLECTION` | `aadamjacobs` | Internet Archive collection ID |
| `OUTPUT_DIR` | `/data/music` | Root directory for FLAC files |
| `STATE_DIR` | `/data/state` | SQLite DB and health file location |
| `SYNC_INTERVAL` | `3600` | Seconds between syncs (`0` = run once) |
| `CONCURRENCY` | `3` | Parallel downloads (keep ≤ 5 to be polite) |
| `RATE_LIMIT_DELAY` | `1.0` | Extra delay between requests per worker |
| `REQUEST_TIMEOUT` | `120` | Per-request HTTP timeout (seconds) |
| `RETRY_COUNT` | `5` | Max retries per download or metadata fetch |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DRY_RUN` | `false` | List what would download, write nothing |
| `CHECKSUM_MANIFEST` | `true` | Write `checksums.md5` per concert folder |
| `WEBHOOK_URL` | *(empty)* | POST a JSON summary here after each sync |
| `HEALTH_PORT` | `6547` | Port for `/health` and `/metrics` |

---

## 🗄️ How state works

| Volume | Path | Contents |
|---|---|---|
| music | `/data/music` | FLAC files organised by concert |
| state | `/data/state` | `mirror.db` (SQLite) |

**SQLite tables:**

- **`items`** — one row per concert: status, folder name, retry count, full IA metadata blob
- **`tracks`** — one row per FLAC file: local path, checksum, download timestamp
- **`sync_runs`** — history of every sync with summary statistics

---

## 🔄 How incremental sync works

1. Every cycle pages through the IA scrape API and `upsert`s every identifier found — existing rows are untouched.
2. The work list is built from rows with `status = pending` **or** `status = failed AND retry_count < RETRY_COUNT`.
3. Items already `complete` or `no_flac` are **excluded at query time** — no HTTP requests made for them.
4. Before opening any socket, the downloader checks whether the local file already exists with a matching size and checksum — if so it skips instantly.
5. Interrupted downloads leave a `.part` file; on the next run an HTTP `Range` header resumes from the last written byte.

---

## 🏗️ Architecture

```
src/
├── config.py       Environment variable config
├── logger.py       structlog → JSON stdout
├── database.py     aiosqlite — items / tracks / sync_runs
├── discovery.py    IA scrape API with cursor pagination
├── metadata.py     IA metadata JSON → ConcertInfo / TrackInfo
├── file_naming.py  Sanitisation, folder & track name generation
├── downloader.py   Async httpx — resume, checksum, retry
├── tagger.py       mutagen FLAC Vorbis comment writer
├── health.py       Threaded /health + /metrics HTTP server
├── sync.py         SyncManager orchestrator
├── scheduler.py    Periodic asyncio loop
└── main.py         Entry point + signal handling
```

---

## 📜 License

MIT — see [LICENSE](LICENSE).
