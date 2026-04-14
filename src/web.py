"""
Web UI + REST API served via FastAPI / uvicorn.

Routes
------
GET /                     → dashboard HTML
GET /health               → JSON health check (Docker HEALTHCHECK target)
GET /api/stats            → summary statistics
GET /api/syncs            → recent sync runs
GET /api/items            → paginated item browser (search, filter by status)
GET /api/items/{id}       → single item + its tracks
GET /api/catalog/refresh  → trigger catalog export on demand
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

from .catalog import export_catalog
from .config import Config
from .database import Database
from .logger import get_logger

log = get_logger(__name__)

# Shared mutable state written by the scheduler, read by the API
_health_state: dict[str, Any] = {
    "status": "starting",
    "started_at": datetime.now(timezone.utc).isoformat(),
}


def set_health(status: str, **extra: Any) -> None:
    _health_state.update({"status": status, **extra})


def create_app(config: Config, db: Database) -> FastAPI:
    app = FastAPI(title="archive-mirror", docs_url=None, redoc_url=None)

    # ── Health ────────────────────────────────────────────────────────────
    @app.get("/health", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def health() -> JSONResponse:
        ok = _health_state.get("status") == "ok"
        return JSONResponse(_health_state, status_code=200 if ok else 503)

    # ── Stats API ─────────────────────────────────────────────────────────
    @app.get("/api/stats")
    async def stats() -> JSONResponse:
        item_counts = await db.count_items_by_status()
        track_counts = await db.count_tracks()
        last_sync = await db.get_last_sync()
        return JSONResponse(
            {
                "items": item_counts,
                "tracks": track_counts,
                "last_sync": last_sync,
                "collections": config.collections,
                "health": _health_state,
            }
        )

    # ── Recent syncs ──────────────────────────────────────────────────────
    @app.get("/api/syncs")
    async def recent_syncs(limit: int = Query(default=10, le=100)) -> JSONResponse:
        syncs = await db.get_recent_syncs(limit=limit)
        return JSONResponse(syncs)

    # ── Items browser ─────────────────────────────────────────────────────
    @app.get("/api/items")
    async def items_list(
        q: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
        collection: Optional[str] = Query(default=None),
        page: int = Query(default=1, ge=1),
        per_page: int = Query(default=50, le=200),
    ) -> JSONResponse:
        rows, total = await db.get_items_paginated(
            status=status,
            collection=collection,
            search=q,
            page=page,
            per_page=per_page,
        )
        return JSONResponse(
            {
                "items": rows,
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": max(1, (total + per_page - 1) // per_page),
            }
        )

    # ── Single item ───────────────────────────────────────────────────────
    @app.get("/api/items/{identifier:path}")
    async def item_detail(identifier: str) -> JSONResponse:
        item = await db.get_item(identifier)
        if item is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        tracks = await db.get_tracks_for_item(identifier)
        # Don't expose the full raw_metadata blob in the detail view
        item.pop("raw_metadata", None)
        return JSONResponse({"item": item, "tracks": tracks})

    # ── On-demand catalog export ──────────────────────────────────────────
    @app.get("/api/catalog/refresh")
    async def catalog_refresh() -> JSONResponse:
        counts = await export_catalog(config, db)
        return JSONResponse({"status": "ok", **counts})

    # ── Dashboard HTML ────────────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    return app


# ---------------------------------------------------------------------------
# Dashboard — single-file SPA (Tailwind CDN + Alpine.js + vanilla fetch)
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>archive-mirror</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
  <style>
    [x-cloak] { display: none !important; }
    .status-complete   { @apply bg-green-100 text-green-800; }
    .status-pending    { @apply bg-yellow-100 text-yellow-800; }
    .status-failed     { @apply bg-red-100 text-red-800; }
    .status-no_flac    { @apply bg-gray-100 text-gray-600; }
    .status-downloading{ @apply bg-blue-100 text-blue-800; }
  </style>
</head>
<body class="bg-gray-50 text-gray-900 min-h-screen" x-data="app()" x-init="init()">

<!-- Header -->
<header class="bg-gray-900 text-white shadow-lg">
  <div class="max-w-7xl mx-auto px-4 py-4 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <span class="text-2xl">🎙️</span>
      <div>
        <h1 class="text-xl font-bold tracking-tight">archive-mirror</h1>
        <p class="text-xs text-gray-400" x-text="collections.length ? 'Collections: ' + collections.join(', ') : ''"></p>
      </div>
    </div>
    <div class="flex items-center gap-3">
      <span class="text-xs text-gray-400" x-text="lastRefresh ? 'Updated ' + lastRefresh : ''"></span>
      <span :class="healthDot" class="inline-block w-3 h-3 rounded-full"></span>
      <span class="text-sm font-medium" x-text="healthLabel"></span>
    </div>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 py-8 space-y-8">

  <!-- Stats cards -->
  <section>
    <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-4">Library overview</h2>
    <div class="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
      <template x-for="card in statCards" :key="card.label">
        <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-4 flex flex-col items-center">
          <span class="text-3xl font-bold" :class="card.color" x-text="card.value.toLocaleString()"></span>
          <span class="text-xs text-gray-500 mt-1 text-center" x-text="card.label"></span>
        </div>
      </template>
    </div>
  </section>

  <!-- Last sync summary -->
  <section x-show="lastSync" x-cloak>
    <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide mb-4">Last sync</h2>
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 p-5 grid grid-cols-2 sm:grid-cols-4 gap-4 text-sm">
      <div><p class="text-gray-500 text-xs mb-1">Started</p><p class="font-medium" x-text="fmtDate(lastSync?.started_at)"></p></div>
      <div><p class="text-gray-500 text-xs mb-1">Duration</p><p class="font-medium" x-text="syncDuration(lastSync)"></p></div>
      <div><p class="text-gray-500 text-xs mb-1">Tracks downloaded</p><p class="font-medium text-green-700" x-text="(lastSync?.tracks_downloaded || 0).toLocaleString()"></p></div>
      <div><p class="text-gray-500 text-xs mb-1">Failures</p><p class="font-medium" :class="lastSync?.tracks_failed > 0 ? 'text-red-600' : 'text-gray-700'" x-text="lastSync?.tracks_failed || 0"></p></div>
    </div>
  </section>

  <!-- Recent sync history -->
  <section>
    <div class="flex items-center justify-between mb-4">
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide">Sync history</h2>
      <button @click="loadSyncs()" class="text-xs text-indigo-600 hover:underline">Refresh</button>
    </div>
    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <table class="min-w-full text-sm">
        <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
          <tr>
            <th class="px-4 py-3 text-left">Started</th>
            <th class="px-4 py-3 text-right">Discovered</th>
            <th class="px-4 py-3 text-right">Completed</th>
            <th class="px-4 py-3 text-right">Tracks</th>
            <th class="px-4 py-3 text-right">Failed</th>
            <th class="px-4 py-3 text-left">Status</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <template x-if="syncs.length === 0">
            <tr><td colspan="6" class="px-4 py-6 text-center text-gray-400">No sync runs yet</td></tr>
          </template>
          <template x-for="s in syncs" :key="s.id">
            <tr class="hover:bg-gray-50">
              <td class="px-4 py-3 font-mono text-xs" x-text="fmtDate(s.started_at)"></td>
              <td class="px-4 py-3 text-right" x-text="(s.items_discovered||0).toLocaleString()"></td>
              <td class="px-4 py-3 text-right text-green-700 font-medium" x-text="(s.items_completed||0).toLocaleString()"></td>
              <td class="px-4 py-3 text-right" x-text="(s.tracks_downloaded||0).toLocaleString()"></td>
              <td class="px-4 py-3 text-right" :class="s.tracks_failed > 0 ? 'text-red-600 font-medium' : ''" x-text="s.tracks_failed||0"></td>
              <td class="px-4 py-3">
                <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                      :class="s.status === 'complete' ? 'bg-green-100 text-green-800' : s.status === 'running' ? 'bg-blue-100 text-blue-800' : 'bg-red-100 text-red-800'"
                      x-text="s.status"></span>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>

  <!-- Items browser -->
  <section>
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide">Concert library</h2>
      <div class="flex gap-2 flex-wrap">
        <input
          x-model="search"
          @input.debounce.400ms="fetchItems(1)"
          type="text"
          placeholder="Search artist, title, venue…"
          class="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 w-64"
        />
        <select x-model="filterStatus" @change="fetchItems(1)"
                class="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300">
          <option value="">All statuses</option>
          <option value="complete">Complete</option>
          <option value="pending">Pending</option>
          <option value="failed">Failed</option>
          <option value="no_flac">No FLAC</option>
          <option value="downloading">Downloading</option>
        </select>
      </div>
    </div>

    <div class="bg-white rounded-xl shadow-sm border border-gray-100 overflow-hidden">
      <table class="min-w-full text-sm">
        <thead class="bg-gray-50 text-xs text-gray-500 uppercase tracking-wide">
          <tr>
            <th class="px-4 py-3 text-left">Artist</th>
            <th class="px-4 py-3 text-left">Date</th>
            <th class="px-4 py-3 text-left hidden md:table-cell">Venue</th>
            <th class="px-4 py-3 text-left hidden lg:table-cell">Collection</th>
            <th class="px-4 py-3 text-left">Status</th>
            <th class="px-4 py-3 text-left hidden sm:table-cell">Folder</th>
            <th class="px-4 py-3 text-right">IA</th>
          </tr>
        </thead>
        <tbody class="divide-y divide-gray-100">
          <template x-if="itemsLoading">
            <tr><td colspan="7" class="px-4 py-8 text-center text-gray-400">Loading…</td></tr>
          </template>
          <template x-if="!itemsLoading && items.length === 0">
            <tr><td colspan="7" class="px-4 py-8 text-center text-gray-400">No items found</td></tr>
          </template>
          <template x-for="item in items" :key="item.identifier">
            <tr class="hover:bg-gray-50 cursor-pointer" @click="openItem(item)">
              <td class="px-4 py-3 font-medium" x-text="item.artist || '—'"></td>
              <td class="px-4 py-3 font-mono text-xs" x-text="item.date || '—'"></td>
              <td class="px-4 py-3 text-gray-500 hidden md:table-cell" x-text="item.venue || '—'"></td>
              <td class="px-4 py-3 text-gray-500 text-xs hidden lg:table-cell" x-text="item.collection || '—'"></td>
              <td class="px-4 py-3">
                <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                      :class="statusClass(item.status)"
                      x-text="item.status"></span>
              </td>
              <td class="px-4 py-3 text-xs text-gray-400 font-mono hidden sm:table-cell truncate max-w-xs" x-text="item.folder_name || '—'"></td>
              <td class="px-4 py-3 text-right">
                <a :href="'https://archive.org/details/' + item.identifier"
                   target="_blank"
                   @click.stop
                   class="text-indigo-500 hover:text-indigo-700 text-xs">↗</a>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <!-- Pagination -->
      <div class="px-4 py-3 border-t border-gray-100 flex items-center justify-between text-sm text-gray-500">
        <span x-text="'Showing ' + items.length + ' of ' + totalItems.toLocaleString() + ' items'"></span>
        <div class="flex gap-2">
          <button :disabled="currentPage <= 1"
                  @click="fetchItems(currentPage - 1)"
                  class="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">← Prev</button>
          <span class="px-3 py-1" x-text="'Page ' + currentPage + ' of ' + totalPages"></span>
          <button :disabled="currentPage >= totalPages"
                  @click="fetchItems(currentPage + 1)"
                  class="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">Next →</button>
        </div>
      </div>
    </div>
  </section>

</main>

<!-- Item detail modal -->
<div x-show="selectedItem" x-cloak
     class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
     @click.self="selectedItem = null">
  <div class="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[80vh] overflow-y-auto p-6" x-show="selectedItem">
    <div class="flex items-start justify-between mb-4">
      <div>
        <h3 class="text-lg font-bold" x-text="selectedItem?.artist + ' — ' + selectedItem?.date"></h3>
        <p class="text-sm text-gray-500" x-text="selectedItem?.venue"></p>
      </div>
      <button @click="selectedItem = null" class="text-gray-400 hover:text-gray-600 text-2xl leading-none">&times;</button>
    </div>
    <div class="flex gap-2 mb-4 flex-wrap">
      <span class="px-2 py-0.5 rounded-full text-xs font-medium" :class="statusClass(selectedItem?.status)" x-text="selectedItem?.status"></span>
      <span class="text-xs text-gray-400 font-mono" x-text="selectedItem?.identifier"></span>
    </div>
    <div class="mb-4" x-show="selectedItem?.folder_name">
      <p class="text-xs text-gray-500 mb-1">Local folder</p>
      <code class="text-xs bg-gray-100 rounded px-2 py-1 block" x-text="selectedItem?.folder_name"></code>
    </div>
    <div x-show="selectedTracks.length > 0">
      <p class="text-xs text-gray-500 mb-2 uppercase tracking-wide font-semibold">Tracks (<span x-text="selectedTracks.length"></span>)</p>
      <ul class="space-y-1">
        <template x-for="t in selectedTracks" :key="t.id">
          <li class="flex items-center gap-2 text-sm">
            <span class="text-gray-400 w-6 text-right text-xs" x-text="t.track_number || ''"></span>
            <span x-text="t.title || t.local_filename || t.ia_filename" class="flex-1"></span>
            <span class="text-xs px-1.5 py-0.5 rounded" :class="t.status === 'complete' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500'" x-text="t.status"></span>
          </li>
        </template>
      </ul>
    </div>
    <div class="mt-5 pt-4 border-t border-gray-100 flex justify-end">
      <a :href="'https://archive.org/details/' + selectedItem?.identifier"
         target="_blank"
         class="text-sm text-indigo-600 hover:underline">View on Internet Archive ↗</a>
    </div>
  </div>
</div>

<script>
function app() {
  return {
    // health
    healthStatus: 'starting',
    healthLabel: 'Starting…',
    healthDot: 'bg-yellow-400',
    collections: [],
    lastRefresh: '',

    // stats
    statCards: [],
    lastSync: null,

    // sync history
    syncs: [],

    // items browser
    items: [],
    totalItems: 0,
    currentPage: 1,
    totalPages: 1,
    search: '',
    filterStatus: '',
    itemsLoading: false,

    // item detail
    selectedItem: null,
    selectedTracks: [],

    async init() {
      await Promise.all([this.loadStats(), this.loadSyncs(), this.fetchItems(1)]);
      // Auto-refresh every 30 seconds
      setInterval(() => {
        this.loadStats();
        this.loadSyncs();
      }, 30000);
    },

    async loadStats() {
      try {
        const r = await fetch('/api/stats');
        const d = await r.json();

        this.collections = d.collections || [];
        this.lastSync = d.last_sync;
        this.lastRefresh = new Date().toLocaleTimeString();

        const h = d.health || {};
        this.healthStatus = h.status || 'unknown';
        this.healthLabel = h.sync_status || h.status || 'unknown';
        this.healthDot = h.status === 'ok' ? 'bg-green-400' : h.status === 'starting' ? 'bg-yellow-400' : 'bg-red-500';

        const items = d.items || {};
        const tracks = d.tracks || {};
        this.statCards = [
          { label: 'Discovered',   value: Object.values(items).reduce((a,b) => a+b, 0), color: 'text-gray-800' },
          { label: 'Complete',     value: items.complete || 0,     color: 'text-green-600' },
          { label: 'Pending',      value: items.pending || 0,      color: 'text-yellow-600' },
          { label: 'No FLAC',      value: items.no_flac || 0,      color: 'text-gray-500' },
          { label: 'Failed',       value: items.failed || 0,       color: 'text-red-600' },
          { label: 'Tracks DL\'d', value: tracks.complete || 0,    color: 'text-indigo-600' },
        ];
      } catch(e) { console.error('stats error', e); }
    },

    async loadSyncs() {
      try {
        const r = await fetch('/api/syncs?limit=8');
        this.syncs = await r.json();
      } catch(e) { console.error('syncs error', e); }
    },

    async fetchItems(page) {
      this.itemsLoading = true;
      this.currentPage = page;
      try {
        const params = new URLSearchParams({ page, per_page: 50 });
        if (this.search) params.set('q', this.search);
        if (this.filterStatus) params.set('status', this.filterStatus);
        const r = await fetch('/api/items?' + params);
        const d = await r.json();
        this.items = d.items || [];
        this.totalItems = d.total || 0;
        this.totalPages = d.pages || 1;
      } catch(e) { console.error('items error', e); }
      this.itemsLoading = false;
    },

    async openItem(item) {
      this.selectedItem = item;
      this.selectedTracks = [];
      try {
        const r = await fetch('/api/items/' + item.identifier);
        const d = await r.json();
        this.selectedItem = d.item;
        this.selectedTracks = d.tracks || [];
      } catch(e) { console.error('item detail error', e); }
    },

    statusClass(status) {
      const map = {
        complete: 'bg-green-100 text-green-800',
        pending:  'bg-yellow-100 text-yellow-800',
        failed:   'bg-red-100 text-red-800',
        no_flac:  'bg-gray-100 text-gray-600',
        downloading: 'bg-blue-100 text-blue-800',
      };
      return map[status] || 'bg-gray-100 text-gray-600';
    },

    fmtDate(iso) {
      if (!iso) return '—';
      try { return new Date(iso).toLocaleString(); } catch { return iso; }
    },

    syncDuration(s) {
      if (!s?.started_at || !s?.completed_at) return s?.status === 'running' ? 'Running…' : '—';
      const ms = new Date(s.completed_at) - new Date(s.started_at);
      const m = Math.floor(ms / 60000);
      const sec = Math.floor((ms % 60000) / 1000);
      return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
    },
  };
}
</script>
</body>
</html>"""
