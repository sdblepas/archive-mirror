# 🎙️ archive-mirror

[![Build & Publish Docker](https://github.com/sdblepas/archive-mirror/actions/workflows/build.yml/badge.svg)](https://github.com/sdblepas/archive-mirror/actions/workflows/build.yml)
[![Docker Pulls](https://img.shields.io/docker/pulls/sdblepas/archive-mirror)](https://hub.docker.com/r/sdblepas/archive-mirror)
[![Docker Image Size](https://img.shields.io/docker/image-size/sdblepas/archive-mirror/latest)](https://hub.docker.com/r/sdblepas/archive-mirror)
[![Docker Version](https://img.shields.io/docker/v/sdblepas/archive-mirror?sort=semver&label=version)](https://hub.docker.com/r/sdblepas/archive-mirror/tags)
[![Python](https://img.shields.io/badge/python-3.12-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/github/license/sdblepas/archive-mirror)](LICENSE)
[![Last Commit](https://img.shields.io/github/last-commit/sdblepas/archive-mirror)](https://github.com/sdblepas/archive-mirror/commits/main)

> Un service Docker autohébergé qui télécharge automatiquement la **Collection Aadam Jacobs** depuis l'Internet Archive — en FLAC, avec tags complets, en gardant votre bibliothèque à jour.

---

## 🎸 La Collection Aadam Jacobs

Aadam Jacobs est le légendaire « Taping Guy » de Chicago. Pendant plus de vingt ans, il a assisté à une douzaine de concerts par mois dans les salles mythiques de la ville — **Lounge Ax**, **The Metro**, **le Double Door**, le **Smart Bar** — en captant ce que personne d'autre n'enregistrait. Ses bandes documentent des performances de groupes qui ont ensuite défini le rock alternatif : **Nirvana, Sonic Youth, The Flaming Lips**, et des milliers d'autres.

Son archive couvre environ **10 000 cassettes** — soit approximativement **30 000 concerts** — enregistrés des années 1980 aux années 2000, d'abord sur cassette, puis DAT, puis numérique.

> *« Ma passion, c'est vraiment de documenter quelque chose qui ne serait pas documenté autrement. C'est plus un désir de collecter et d'archiver ces moments. »*
> — Aadam Jacobs, Glorious Noise, 2004

En partenariat avec le **[Live Music Archive de l'Internet Archive](https://archive.org/details/aadamjacobs)**, la collection est en cours de numérisation par une équipe de bénévoles depuis l'automne 2024. Son travail a été mis en lumière dans le documentaire **Melomaniac** (2023, réal. Katlin Schneider) et dans un reportage sur la radio publique de Chicago (WBEZ).

Cet outil existe pour rendre cette archive disponible en permanence sur votre propre matériel.

---

## ✨ Fonctionnalités

- 🔍 **Découverte complète** — pagination par curseur, supporte 30 000+ éléments
- ⬇️ **Téléchargement FLAC uniquement** — ignore les concerts sans audio lossless
- ♻️ **Synchronisation incrémentale** — télécharge uniquement ce qui est nouveau, reprise sans perte après un arrêt
- 📂 **Structure de dossiers propre** — `Artiste - AAAA-MM-JJ / 01 - Titre - Artiste.flac`
- 🏷️ **Tags automatiques** — écrit `TITLE`, `ARTIST`, `ALBUM`, `DATE`, `VENUE`, `TRACKNUMBER`
- ✅ **Validation par checksum** — MD5/SHA-1 vérifiés contre les métadonnées de l'Internet Archive
- ⏸️ **Reprise des téléchargements** — les téléchargements interrompus reprennent là où ils s'étaient arrêtés
- 🔁 **Logique de retry** — back-off exponentiel configurable
- 🩺 **Endpoint de santé** — `GET /health` et `GET /metrics` sur le port `6547`
- 🧾 **État SQLite** — historique complet de chaque concert et piste, survit aux redémarrages
- 🐳 **Natif Docker** — un seul `docker compose up -d` suffit

---

## 🚀 Installation

### Prérequis

- Docker ≥ 24
- Docker Compose v2
- 500 Go+ d'espace disque (l'archive complète est volumineuse)

### 1 — Créer les répertoires de données

Sur votre hôte (adaptez le chemin à votre configuration) :

```bash
mkdir -p /volume1/Docker/archive-mirror/music
mkdir -p /volume1/Docker/archive-mirror/state
```

### 2 — Créer le fichier `docker-compose.yml`

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
      SYNC_INTERVAL: "3600"      # secondes entre chaque sync (0 = une seule fois)
      CONCURRENCY: "3"           # téléchargements parallèles
      RATE_LIMIT_DELAY: "1.0"    # délai entre requêtes par worker
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

### 3 — Démarrer le service

```bash
docker compose up -d
```

### 4 — Suivre les logs

```bash
docker compose logs -f
```

Vous verrez des logs JSON structurés comme ceci :

```json
{"event": "sync.start", "collection": "aadamjacobs", "dry_run": false}
{"event": "sync.discovery_complete", "discovered": 412, "new": 412}
{"event": "sync.processing", "identifier": "aj1990-11-09", "artist": "Nirvana", "tracks": 14}
{"event": "download.complete", "filename": "01 - Blew - Nirvana.flac", "bytes": 42817234}
{"event": "sync.item_complete", "folder": "Nirvana - 1990-11-09", "tracks": 14}
{"event": "sync.complete", "tracks_downloaded": 3890, "failures": 0}
```

### 5 — Vérifier la santé du service

```bash
curl http://localhost:6547/health
# {"status": "ok", "sync_status": "sleeping", "next_sync_in_seconds": 3542}

curl http://localhost:6547/metrics
# {"last_sync": {"items_discovered": 412, "tracks_downloaded": 3890, "failures": 0}}
```

---

## 📁 Structure des fichiers

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

## ⚙️ Référence de configuration

| Variable | Défaut | Description |
|---|---|---|
| `COLLECTION` | `aadamjacobs` | Identifiant de la collection Internet Archive |
| `OUTPUT_DIR` | `/data/music` | Répertoire racine pour les fichiers FLAC |
| `STATE_DIR` | `/data/state` | Emplacement de la base SQLite |
| `SYNC_INTERVAL` | `3600` | Secondes entre les syncs (`0` = une seule fois) |
| `CONCURRENCY` | `3` | Téléchargements parallèles (garder ≤ 5 par courtoisie) |
| `RATE_LIMIT_DELAY` | `1.0` | Délai supplémentaire entre requêtes par worker |
| `REQUEST_TIMEOUT` | `120` | Timeout HTTP par requête (secondes) |
| `RETRY_COUNT` | `5` | Nombre max de tentatives par téléchargement |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DRY_RUN` | `false` | Lister ce qui serait téléchargé sans rien écrire |
| `CHECKSUM_MANIFEST` | `true` | Écrire `checksums.md5` dans chaque dossier |
| `WEBHOOK_URL` | *(vide)* | POST un résumé JSON ici après chaque sync |
| `HEALTH_PORT` | `6547` | Port pour `/health` et `/metrics` |

---

## 🗄️ Fonctionnement de l'état

| Volume | Chemin | Contenu |
|---|---|---|
| music | `/data/music` | Fichiers FLAC organisés par concert |
| state | `/data/state` | `mirror.db` (SQLite) |

**Tables SQLite :**

- **`items`** — un concert par ligne : statut, nom du dossier, compteur de retry, métadonnées IA complètes
- **`tracks`** — un fichier FLAC par ligne : chemin local, checksum, horodatage du téléchargement
- **`sync_runs`** — historique de chaque synchronisation avec statistiques

---

## 🔄 Fonctionnement de la synchronisation incrémentale

1. À chaque cycle, l'API scrape de l'IA est paginée et chaque identifiant trouvé est inséré via `upsert` — les lignes existantes ne sont pas touchées.
2. La liste de travail ne contient que les lignes avec `status = pending` **ou** `status = failed AND retry_count < RETRY_COUNT`.
3. Les éléments déjà `complete` ou `no_flac` sont **exclus dès la requête SQL** — aucune requête HTTP n'est faite pour eux.
4. Avant d'ouvrir la moindre connexion, le téléchargeur vérifie si le fichier local existe déjà avec la bonne taille et le bon checksum — si oui, il passe immédiatement au suivant.
5. Les téléchargements interrompus laissent un fichier `.part` ; à la prochaine exécution, un en-tête HTTP `Range` reprend depuis le dernier octet écrit.

---

## 🏗️ Architecture

```
src/
├── config.py       Configuration par variables d'environnement
├── logger.py       structlog → JSON stdout
├── database.py     aiosqlite — items / tracks / sync_runs
├── discovery.py    API scrape IA avec pagination par curseur
├── metadata.py     JSON metadata IA → ConcertInfo / TrackInfo
├── file_naming.py  Sanitisation, génération des noms de fichiers
├── downloader.py   Async httpx — reprise, checksum, retry
├── tagger.py       Écriture des tags Vorbis FLAC via mutagen
├── health.py       Serveur HTTP /health + /metrics (thread dédié)
├── sync.py         Orchestrateur SyncManager
├── scheduler.py    Boucle asyncio périodique
└── main.py         Point d'entrée + gestion des signaux
```

---

## 📜 Licence

MIT — voir [LICENSE](LICENSE).
