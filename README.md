# luduclone

[![docker](https://github.com/rangoDJ/luduclone/actions/workflows/docker.yml/badge.svg)](https://github.com/rangoDJ/luduclone/actions/workflows/docker.yml)
[![windows-client](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml/badge.svg)](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Self-hosted game-save sync, **Windows → Linux**, built on the
[ludusavi manifest](https://github.com/mtkennerly/ludusavi-manifest).

> **Prebuilt artifacts:** CI publishes the server image to
> `ghcr.io/rangodj/luduclone:latest` and builds `luduclone.exe` (CLI) +
> `luduclone-gui.exe` (GUI) — download from the latest
> [windows-client run](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml),
> or from tagged releases.

A Docker-based server is the hub; thin clients on each OS back up saves locally,
upload them, and download them on the other machine for restore. Windows-only
games run via Steam Proton are restored by **re-rooting** their Windows save
paths into the right Proton prefix.

## Architecture

```
Windows client  ──upload──►  Server (Docker)  ──download──►  Linux client
  resolve <winAppData> etc.    REST API + SQLite              resolve target:
  glob + tar bundle            save-bundle store              native path, or
  + mapping.json               serves manifest                re-root into Proton prefix
```

The upload records *which manifest placeholder* each save came from, so the
Linux side can retarget it correctly instead of trusting raw `C:\` paths.

## Components

| Path        | What it is                                                        |
|-------------|-------------------------------------------------------------------|
| `shared/`   | Manifest parse + placeholder resolver + filesystem scanner (used by server & clients) |
| `server/`   | FastAPI app, SQLite metadata, bundle storage, Docker packaging    |
| `client/`   | Cross-OS backup/restore client (backup + upload done; restore next)|

## Run the server

```bash
# Pull + run the published image (docker-compose.yml uses ghcr.io by default):
LUDUCLONE_TOKENS="rango:supersecret" docker compose up -d

# Leave LUDUCLONE_TOKENS unset for OPEN auth (dev only — anyone is user "default").

# Build from source instead of pulling (local development):
docker compose -f docker-compose.yml -f docker-compose.build.yml up --build

# Or a plain one-off container:
docker run -d -p 8000:8000 -v luduclone-data:/data \
  -e LUDUCLONE_TOKENS="rango:supersecret" ghcr.io/rangodj/luduclone:latest
```

Then:

```bash
curl localhost:8000/health
curl -H "Authorization: Bearer supersecret" localhost:8000/manifest | head
```

Open **http://localhost:8000/ui** in a browser for a dashboard of uploaded games
and their versions (enter your token in the field; leave blank for open auth).

Local dev without Docker:

```bash
pip install -r requirements.txt
uvicorn server.app:app --reload
```

## API

| Method | Endpoint                          | Purpose                              |
|--------|-----------------------------------|--------------------------------------|
| GET    | `/health`                         | Liveness + auth mode                 |
| GET    | `/manifest`                       | Cached ludusavi manifest (YAML)      |
| GET    | `/games`                          | Games with backups for this user     |
| GET    | `/games/{game}/saves`             | List versions                        |
| POST   | `/games/{game}/saves`             | Upload bundle (multipart) + mapping  |
| DELETE | `/games/{game}`                   | Delete all backups of a game         |
| GET    | `/games/{game}/saves/latest`      | Download newest bundle               |
| GET    | `/games/{game}/saves/{version}`   | Download specific version            |

Auth: `Authorization: Bearer <token>` (skip when `LUDUCLONE_TOKENS` is unset).
Download responses carry metadata in `X-Luduclone-*` headers (source OS, mapping, sha256).

## Client (Windows)

```bash
pip install -r requirements.txt
# one-time setup (stored in your user config dir)
python -m client configure --server http://your-nas:8000 --token secret

python -m client scan                 # preview what's found on this machine
python -m client backup               # bundle + upload everything found
python -m client backup --game Celeste --config   # one game, include config files
python -m client remote               # list what's backed up on the server
```

`scan`/`backup` walk the whole manifest by default to discover installed games;
use `--game` (repeatable) to narrow it. Each upload records *which placeholder*
each file came from, so the Linux side can retarget it.

Prefer a window over the terminal? Run the GUI:

```bash
python -m client.gui        # or just run luduclone-gui.exe
```

It exposes the same Scan / Back up & upload / Server games actions with fields
for the server URL and token.

## Restore on Steam Deck / Linux (Proton)

For Windows games run through Proton, the client injects your saves into the
game's compatibility prefix (`steamapps/compatdata/<appid>/pfx/...`).

```bash
python -m client configure --server http://your-nas:8000 --token secret
python -m client prefixes              # show discovered Steam libs + Proton prefixes
python -m client restore --dry-run     # show where each save would go
python -m client restore               # restore every backed-up game found here
python -m client restore --game Celeste
python -m client restore --mode native # force native Linux paths (official ports)
python -m client restore --no-registry # skip registry merge
```

Restore picks a target automatically per game:
- **proton** — a Steam Proton prefix exists for the game's app id: saves are
  re-rooted into `compatdata/<appid>/pfx`, and captured **registry** keys are
  merged into the prefix's `user.reg`/`system.reg` (originals backed up to
  `*.luduclone-bak`).
- **native** — no prefix, but the manifest has a Linux save path (official Linux
  build): saves go to the native `$XDG_*`/`$HOME` location.

The Proton-targeted game must already be installed on the Deck (so its prefix
exists). Steam libraries on internal storage **and SD card** are discovered
automatically via `libraryfolders.vdf`.

## Roadmap

- [x] Phase 0 — shared manifest engine
- [x] Phase 1 — Dockerized server
- [x] Phase 2 — Windows client: backup + upload
- [x] Phase 4 — Proton prefix re-rooting (Steam Deck restore)
- [x] Phase 3 — native Linux-port restore (`--mode native`/auto)
- [x] Phase 5a — Windows registry capture + Proton `user.reg`/`system.reg` import
- [ ] Phase 5b — conflict/diff before overwrite, web UI, Lutris/Heroic prefixes, single-file client build

### Known limitations
- If a save path contains `<storeUserId>` *inside* the save subtree, restore can't
  resolve a Steam user id on a fresh prefix — those entries are skipped with a
  `skipped-wildcard` note. Most AppData/Documents saves are unaffected.
- Registry import is done by **appending** to `user.reg`/`system.reg` (Wine applies
  last-write-wins). Validate on a real Proton prefix before trusting it widely; a
  standard Windows `.reg` is also renderable for manual import.
