"""
FastAPI application factory.

Responsibilities
----------------
- Create the FastAPI app
- Attach db + config to app.state (routers read them via request.app.state)
- Include all routers
- Serve the dashboard HTML at /

All route logic lives in src/routers/*.
All mutable health state lives in src/web_state.py.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from .config import Config
from .database import Database
from .routers import catalog, health, items, stats, syncs
from .web_state import get_health_state, set_health  # re-exported for scheduler

__all__ = ["create_app", "set_health", "get_health_state"]


def create_app(config: Config, db: Database) -> FastAPI:
    app = FastAPI(
        title="archive-mirror",
        description="Internet Archive collection mirror — dashboard & API",
        version="1.0.0",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # Attach shared objects to app.state so routers can reach them
    # without importing module-level globals.
    app.state.config = config
    app.state.db = db

    # ── Routers ───────────────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(stats.router)
    app.include_router(syncs.router)
    app.include_router(items.router)
    app.include_router(catalog.router)

    # ── Dashboard SPA ─────────────────────────────────────────────────────
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
  <style>[x-cloak]{display:none!important}</style>
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
      <div><p class="text-gray-500 text-xs mb-1">Tracks downloaded</p><p class="font-medium text-green-700" x-text="(lastSync?.tracks_downloaded||0).toLocaleString()"></p></div>
      <div><p class="text-gray-500 text-xs mb-1">Failures</p>
        <p class="font-medium" :class="lastSync?.tracks_failed > 0 ? 'text-red-600' : 'text-gray-700'" x-text="lastSync?.tracks_failed||0"></p></div>
    </div>
  </section>

  <!-- Sync history -->
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
              <td class="px-4 py-3 text-right" :class="s.tracks_failed > 0 ? 'text-red-600 font-medium':''" x-text="s.tracks_failed||0"></td>
              <td class="px-4 py-3">
                <span class="px-2 py-0.5 rounded-full text-xs font-medium"
                  :class="s.status==='complete'?'bg-green-100 text-green-800':s.status==='running'?'bg-blue-100 text-blue-800':'bg-red-100 text-red-800'"
                  x-text="s.status"></span>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>

  <!-- Concert browser -->
  <section>
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-4">
      <h2 class="text-sm font-semibold text-gray-500 uppercase tracking-wide">Concert library</h2>
      <div class="flex gap-2 flex-wrap">
        <input x-model="search" @input.debounce.400ms="fetchItems(1)" type="text"
               placeholder="Search artist, title, venue…"
               class="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-300 w-64"/>
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
              <td class="px-4 py-3 font-medium" x-text="item.artist||'—'"></td>
              <td class="px-4 py-3 font-mono text-xs" x-text="item.date||'—'"></td>
              <td class="px-4 py-3 text-gray-500 hidden md:table-cell" x-text="item.venue||'—'"></td>
              <td class="px-4 py-3 text-gray-500 text-xs hidden lg:table-cell" x-text="item.collection||'—'"></td>
              <td class="px-4 py-3">
                <span class="px-2 py-0.5 rounded-full text-xs font-medium" :class="statusClass(item.status)" x-text="item.status"></span>
              </td>
              <td class="px-4 py-3 text-xs text-gray-400 font-mono hidden sm:table-cell truncate max-w-xs" x-text="item.folder_name||'—'"></td>
              <td class="px-4 py-3 text-right">
                <a :href="'https://archive.org/details/'+item.identifier" target="_blank" @click.stop
                   class="text-indigo-500 hover:text-indigo-700 text-xs">↗</a>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
      <!-- Pagination -->
      <div class="px-4 py-3 border-t border-gray-100 flex items-center justify-between text-sm text-gray-500">
        <span x-text="'Showing '+items.length+' of '+totalItems.toLocaleString()+' items'"></span>
        <div class="flex gap-2">
          <button :disabled="currentPage<=1" @click="fetchItems(currentPage-1)"
                  class="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">← Prev</button>
          <span class="px-3 py-1" x-text="'Page '+currentPage+' of '+totalPages"></span>
          <button :disabled="currentPage>=totalPages" @click="fetchItems(currentPage+1)"
                  class="px-3 py-1 rounded border border-gray-200 disabled:opacity-40 hover:bg-gray-50">Next →</button>
        </div>
      </div>
    </div>
  </section>

</main>

<!-- Item detail modal -->
<div x-show="selectedItem" x-cloak
     class="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"
     @click.self="selectedItem=null">
  <div class="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[80vh] overflow-y-auto p-6">
    <div class="flex items-start justify-between mb-4">
      <div>
        <h3 class="text-lg font-bold" x-text="(selectedItem?.artist||'')+ ' — '+(selectedItem?.date||'')"></h3>
        <p class="text-sm text-gray-500" x-text="selectedItem?.venue||''"></p>
      </div>
      <button @click="selectedItem=null" class="text-gray-400 hover:text-gray-600 text-2xl leading-none">&times;</button>
    </div>
    <div class="flex gap-2 mb-4 flex-wrap">
      <span class="px-2 py-0.5 rounded-full text-xs font-medium" :class="statusClass(selectedItem?.status)" x-text="selectedItem?.status"></span>
      <span class="text-xs text-gray-400 font-mono" x-text="selectedItem?.identifier"></span>
    </div>
    <div class="mb-4" x-show="selectedItem?.folder_name">
      <p class="text-xs text-gray-500 mb-1">Local folder</p>
      <code class="text-xs bg-gray-100 rounded px-2 py-1 block" x-text="selectedItem?.folder_name"></code>
    </div>
    <div x-show="selectedTracks.length>0">
      <p class="text-xs text-gray-500 mb-2 uppercase tracking-wide font-semibold">Tracks (<span x-text="selectedTracks.length"></span>)</p>
      <ul class="space-y-1">
        <template x-for="t in selectedTracks" :key="t.id">
          <li class="flex items-center gap-2 text-sm">
            <span class="text-gray-400 w-6 text-right text-xs" x-text="t.track_number||''"></span>
            <span x-text="t.title||t.local_filename||t.ia_filename" class="flex-1"></span>
            <span class="text-xs px-1.5 py-0.5 rounded"
                  :class="t.status==='complete'?'bg-green-100 text-green-700':'bg-gray-100 text-gray-500'"
                  x-text="t.status"></span>
          </li>
        </template>
      </ul>
    </div>
    <div class="mt-5 pt-4 border-t border-gray-100 flex justify-end">
      <a :href="'https://archive.org/details/'+(selectedItem?.identifier||'')" target="_blank"
         class="text-sm text-indigo-600 hover:underline">View on Internet Archive ↗</a>
    </div>
  </div>
</div>

<script>
function app() {
  return {
    healthStatus:'starting', healthLabel:'Starting…', healthDot:'bg-yellow-400',
    collections:[], lastRefresh:'',
    statCards:[], lastSync:null,
    syncs:[],
    items:[], totalItems:0, currentPage:1, totalPages:1,
    search:'', filterStatus:'', itemsLoading:false,
    selectedItem:null, selectedTracks:[],

    async init() {
      await Promise.all([this.loadStats(), this.loadSyncs(), this.fetchItems(1)]);
      setInterval(() => { this.loadStats(); this.loadSyncs(); }, 30000);
    },

    async loadStats() {
      try {
        const d = await fetch('/api/stats').then(r=>r.json());
        this.collections = d.collections||[];
        this.lastSync = d.last_sync;
        this.lastRefresh = new Date().toLocaleTimeString();
        const h = d.health||{};
        this.healthStatus = h.status||'unknown';
        this.healthLabel  = h.sync_status||h.status||'unknown';
        this.healthDot    = h.status==='ok'?'bg-green-400':h.status==='starting'?'bg-yellow-400':'bg-red-500';
        const it=d.items||{}, tr=d.tracks||{};
        this.statCards=[
          {label:'Discovered',   value:Object.values(it).reduce((a,b)=>a+b,0), color:'text-gray-800'},
          {label:'Complete',     value:it.complete||0,   color:'text-green-600'},
          {label:'Pending',      value:it.pending||0,    color:'text-yellow-600'},
          {label:'No FLAC',      value:it.no_flac||0,    color:'text-gray-500'},
          {label:'Failed',       value:it.failed||0,     color:'text-red-600'},
          {label:"Tracks DL'd",  value:tr.complete||0,   color:'text-indigo-600'},
        ];
      } catch(e){console.error('stats',e);}
    },

    async loadSyncs() {
      try { this.syncs = await fetch('/api/syncs?limit=8').then(r=>r.json()); }
      catch(e){console.error('syncs',e);}
    },

    async fetchItems(page) {
      this.itemsLoading=true; this.currentPage=page;
      try {
        const p=new URLSearchParams({page,per_page:50});
        if(this.search) p.set('q',this.search);
        if(this.filterStatus) p.set('status',this.filterStatus);
        const d=await fetch('/api/items?'+p).then(r=>r.json());
        this.items=d.items||[]; this.totalItems=d.total||0; this.totalPages=d.pages||1;
      } catch(e){console.error('items',e);}
      this.itemsLoading=false;
    },

    async openItem(item) {
      this.selectedItem=item; this.selectedTracks=[];
      try {
        const d=await fetch('/api/items/'+item.identifier).then(r=>r.json());
        this.selectedItem=d.item; this.selectedTracks=d.tracks||[];
      } catch(e){console.error('item detail',e);}
    },

    statusClass(s){
      return {complete:'bg-green-100 text-green-800',pending:'bg-yellow-100 text-yellow-800',
              failed:'bg-red-100 text-red-800',no_flac:'bg-gray-100 text-gray-600',
              downloading:'bg-blue-100 text-blue-800'}[s]||'bg-gray-100 text-gray-600';
    },

    fmtDate(iso){
      if(!iso) return '—';
      try{return new Date(iso).toLocaleString();}catch{return iso;}
    },

    syncDuration(s){
      if(!s?.started_at||!s?.completed_at) return s?.status==='running'?'Running…':'—';
      const ms=new Date(s.completed_at)-new Date(s.started_at);
      const m=Math.floor(ms/60000),sec=Math.floor((ms%60000)/1000);
      return m>0?`${m}m ${sec}s`:`${sec}s`;
    },
  };
}
</script>
</body>
</html>"""
