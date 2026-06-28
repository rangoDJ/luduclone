"""luduclone server: a self-hosted hub for cross-OS game-save sync.

Clients (Windows / Linux) back up locally, upload bundles here, and download
them on the other OS for restore. The server also serves the ludusavi manifest
so all clients agree on save locations.

Run locally:
    uvicorn server.app:app --reload
Run in Docker:
    docker compose up --build
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, UploadFile, Form, File
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse

from shared import manifest as manifest_mod

from .config import config
from .storage import Store
from .web import UI_HTML


def _warm_manifest() -> None:
    """Pre-fetch/refresh the manifest cache so the first client isn't blocked
    waiting on the GitHub download. Best-effort: failures are ignored (the
    /manifest route will retry on demand)."""
    try:
        manifest_mod.load(config.manifest_cache, max_age_seconds=config.MANIFEST_MAX_AGE)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Warm the manifest in the background so startup (and the health check) is
    # never blocked by a slow network fetch.
    threading.Thread(target=_warm_manifest, daemon=True).start()
    yield


app = FastAPI(title="luduclone", version="0.1.1", lifespan=lifespan)
store = Store(config.DATA_DIR)


# --------------------------------------------------------------------------
# Auth: Bearer token -> user. If no tokens configured, everything is "default".
# --------------------------------------------------------------------------
def current_user(authorization: Optional[str] = Header(default=None)) -> str:
    if config.auth_open:
        return "default"
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    user = config.tokens.get(token)
    if not user:
        raise HTTPException(401, "Invalid token")
    return user


# --------------------------------------------------------------------------
# Root + health + manifest
# --------------------------------------------------------------------------
@app.get("/")
def root() -> dict:
    """Service info, so hitting the base URL isn't a bare 404."""
    return {
        "service": "luduclone",
        "version": app.version,
        "auth": "open" if config.auth_open else "token",
        "endpoints": ["/ui", "/health", "/manifest", "/games",
                      "/games/{game}/saves", "/docs"],
        "ui": "/ui",
        "docs": "/docs",
    }


@app.get("/ui", response_class=HTMLResponse)
def ui() -> str:
    """A simple dashboard listing uploaded games. Auth happens client-side via a
    token field (works for both open- and token-auth servers)."""
    return UI_HTML


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "auth": "open" if config.auth_open else "token"}


@app.get("/manifest", response_class=PlainTextResponse)
def get_manifest(_user: str = Depends(current_user)) -> str:
    """Serve the cached ludusavi manifest YAML (refreshed from upstream as needed)."""
    manifest_mod.load(config.manifest_cache, max_age_seconds=config.MANIFEST_MAX_AGE)
    return config.manifest_cache.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Saves
# --------------------------------------------------------------------------
@app.get("/games")
def list_games(user: str = Depends(current_user)) -> dict:
    return {"games": store.list_games(user)}


@app.delete("/games/{game}")
def delete_game(game: str, user: str = Depends(current_user)) -> dict:
    """Remove all backups of a game for this user (e.g. to clear a bad upload)."""
    removed = store.delete_game(user, game)
    if removed == 0:
        raise HTTPException(404, "No backups for this game")
    return {"game": game, "deleted_versions": removed}


@app.get("/games/{game}/saves")
def list_saves(game: str, user: str = Depends(current_user)) -> dict:
    versions = [r.to_public() for r in store.list_versions(user, game)]
    if not versions:
        raise HTTPException(404, "No saves for this game")
    return {"game": game, "versions": versions}


@app.post("/games/{game}/saves")
async def upload_save(
    game: str,
    source_os: str = Form(...),
    mapping: str = Form("{}"),
    retain: int = Form(0),
    bundle: UploadFile = File(...),
    user: str = Depends(current_user),
) -> dict:
    """Accept a save bundle (.tar.gz) plus a JSON mapping describing how each
    file maps back to a manifest placeholder, so the other OS can retarget it.

    ``retain`` is the client's requested version cap; combined with the server's
    ``LUDUCLONE_RETAIN`` default, the stricter (smaller positive) limit prunes
    older versions after this one is stored.
    """
    if source_os not in ("windows", "linux"):
        raise HTTPException(400, "source_os must be 'windows' or 'linux'")
    try:
        json.loads(mapping)
    except json.JSONDecodeError:
        raise HTTPException(400, "mapping must be valid JSON")

    version = store.next_version(user, game)
    dest = store.bundle_path(user, game, version)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Stream to a temp file in the same dir, hashing as we go, then atomic rename.
    sha = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:  # take ownership of the fd so it's closed
            while chunk := await bundle.read(1024 * 1024):
                sha.update(chunk)
                size += len(chunk)
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "Empty bundle")
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    rec = store.add_save(
        user=user, game=game, version=version, source_os=source_os,
        sha256=sha.hexdigest(), size=size, mapping=mapping, path=dest,
    )
    keep = _effective_retain(retain, config.RETAIN)
    pruned = store.prune(user, game, keep) if keep else []
    return {"game": game, **rec.to_public(), "pruned": pruned}


def _effective_retain(requested: int, default: int) -> int:
    """The stricter of the client's request and the server default; 0 (either
    side) means 'no limit from that side'."""
    limits = [n for n in (requested, default) if n and n > 0]
    return min(limits) if limits else 0


@app.get("/games/{game}/saves/latest")
def download_latest(game: str, user: str = Depends(current_user)):
    return _download(user, game, None)


@app.get("/games/{game}/saves/{version}")
def download_version(game: str, version: int, user: str = Depends(current_user)):
    return _download(user, game, version)


def _download(user: str, game: str, version: Optional[int]):
    rec = store.get_save(user, game, version)
    if not rec:
        raise HTTPException(404, "Save not found")
    path = store.abs_path(rec)
    if not path.exists():
        raise HTTPException(410, "Bundle missing on disk")
    # Metadata travels in headers so the client can retarget without a second call.
    headers = {
        "X-Luduclone-Version": str(rec.version),
        "X-Luduclone-Source-Os": rec.source_os,
        "X-Luduclone-Sha256": rec.sha256,
        "X-Luduclone-Mapping": json.dumps(json.loads(rec.mapping)),
    }
    return FileResponse(
        path, media_type="application/gzip",
        filename=f"{game}-v{rec.version}.tar.gz", headers=headers,
    )
