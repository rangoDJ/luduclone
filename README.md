# luduclone

[![docker](https://github.com/rangoDJ/luduclone/actions/workflows/docker.yml/badge.svg)](https://github.com/rangoDJ/luduclone/actions/workflows/docker.yml)
[![windows-client](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml/badge.svg)](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Self-hosted game-save sync, **Windows → Linux**, built on the
[ludusavi manifest](https://github.com/mtkennerly/ludusavi-manifest).

> **Prebuilt artifacts:** CI publishes the server image to
> `ghcr.io/rangodj/luduclone:latest`, builds `luduclone.exe` (CLI) +
> `luduclone-gui.exe` (GUI) for Windows, and a single-file `luduclone` binary for
> Linux / **SteamOS (Steam Deck)** — download from the latest
> [windows-client](https://github.com/rangoDJ/luduclone/actions/workflows/windows-client.yml)
> / [linux-client](https://github.com/rangoDJ/luduclone/actions/workflows/linux-client.yml)
> run, or from tagged releases.

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
| POST   | `/games/{game}/saves`             | Upload bundle (multipart) + mapping (+ optional `retain` cap) |
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

# keep at most 5 versions per game on the server (0 = unlimited)
python -m client configure --server http://nas:8000 --token secret --retain 5

# custom games / restore redirects / backup ignore globs (stored locally)
python -m client custom list
python -m client custom add-game "My Game" --path "<home>/.myg/saves" --steam-id 12345
python -m client custom add-redirect "C:/Users/old" "C:/Users/new"
python -m client custom add-ignore "*/cache/*"
```

`scan`/`backup` walk the whole manifest by default to discover installed games;
use `--game` (repeatable) to narrow it. Each upload records *which placeholder*
each file came from, so the Linux side can retarget it.

Prefer a window over the terminal? Run the GUI:

```bash
python -m client.gui        # or just run luduclone-gui.exe
```

The GUI uses a modern Windows 11 (Fluent) look via the Sun Valley theme, Segoe
UI, and DPI-aware (crisp) rendering, with a **View** menu to switch light/dark
(defaults to your Windows setting). From source, `pip install sv-ttk darkdetect`
for the themed look; without them it falls back to the default Tk theme.

Like ludusavi, each tab is a searchable, checkable game list with an expandable
file tree (per save entry, with sizes) and a running summary of what's selected.
Three tabs share one server connection:
- **Back up** — **Scan this PC** lists every game with saves found here (all
  ticked by default); untick what you don't want and **Back up checked**. The
  **Keep last N** spinner caps retained versions per game (0 = unlimited).
- **Restore** — once a server is configured, **Refresh from server** lists
  available backups. **Preview** shows a per-file diff (new / changed /
  unchanged) before you overwrite anything; **Restore checked** then writes only
  the changed/new files and stashes anything it overwrites in an undo dir. A mode
  dropdown (auto/proton/native/windows) picks where saves land.
- **Custom** — define games not in the manifest (name + save paths + registry),
  add restore **redirects** (rewrite a path prefix on the restore machine), and
  **ignore** globs (skip matching files on backup). Saved immediately; re-scan to
  apply.

Restoring **on Windows** writes saves back to their original Windows locations;
**on the Steam Deck** it routes them into the Proton prefix (or native paths).

### Updating the client

The packaged exes self-update from GitHub releases. The GUI checks on startup and
via **Help → Check for updates**; the CLI has:

```bash
luduclone update          # report whether a newer release exists
luduclone update --apply  # download it and swap the exe in place (restart after)
luduclone --version
```

Self-update only applies to the built executables — running from source, it just
reports the available version (use `git pull`).

## Restore on Steam Deck / Linux (Proton)

For Windows games run through Proton, the client injects your saves into the
game's compatibility prefix (`steamapps/compatdata/<appid>/pfx/...`).

**Easiest on a Steam Deck — grab the prebuilt binary** (Desktop Mode → Konsole):

```bash
cd ~ && curl -L -o luduclone \
  https://github.com/rangoDJ/luduclone/releases/latest/download/luduclone
chmod +x luduclone                     # release assets don't keep the +x bit
./luduclone configure --server http://your-nas:8000 --token secret
./luduclone prefixes                   # sanity check: detected libs + prefixes
./luduclone restore --preview          # per-file diff, writes nothing
./luduclone restore                    # restore everything backed up
```

`~` survives SteamOS updates, and the binary self-updates (`./luduclone update
--apply`). No Python/venv needed. The commands below use the from-source form
(`python -m client …`); with the binary, substitute `./luduclone …`.

```bash
python -m client configure --server http://your-nas:8000 --token secret
python -m client prefixes              # show discovered Steam libs + Proton prefixes
python -m client restore --dry-run     # show where each save would go
python -m client restore --preview     # per-file diff (new/changed/unchanged), no writes
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
- [x] Phase 6 — Steam roots: detect installed games + resolve `<base>`/`<root>`/`<game>`
- [x] Phase 5b — conflict/diff preview before overwrite, custom games + redirects
      + ignore filters, retention limit, ludusavi-style checkable GUI
- [ ] Phase 5c — Lutris/Heroic/GOG roots, single-file client build

### Steam roots / installed-game detection

Like ludusavi, luduclone anchors install-dir saves to a real **root**. It parses
every `appmanifest_*.acf` across all Steam libraries (internal + SD card; Steam
path found via the Windows registry or default install dirs) to learn each
installed game's install directory and Proton prefix. This is what lets `<base>`
(the game's install folder) resolve correctly — on backup it finds those saves,
and on restore it routes them back to the install dir while Windows paths go into
the Proton prefix. Run `luduclone prefixes` to see detected libraries + games.

### Known limitations
- Only **Steam** roots so far (covers the Steam Deck case). Lutris/Heroic/GOG
  roots aren't enumerated yet, so `<base>` saves for non-Steam launchers are
  skipped rather than mis-targeted.
- If a save path contains `<storeUserId>` *inside* the save subtree, restore can't
  resolve a Steam user id on a fresh prefix — those entries are skipped with a
  `skipped-wildcard` note. Most AppData/Documents saves are unaffected.
- Registry import is done by **appending** to `user.reg`/`system.reg` (Wine applies
  last-write-wins). Validate on a real Proton prefix before trusting it widely; a
  standard Windows `.reg` is also renderable for manual import.
