"""Backup flow: scan installed games' saves, bundle, and upload."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

from shared import placeholders as ph
from shared.manifest import DEFAULT_TAGS, Manifest

from .api import ApiClient
from .bundle import build_game_bundle
from .roots import SteamIndex
from . import winregistry


def detect_env() -> ph.Env:
    import os
    return ph.Env.detect_windows() if os.name == "nt" else ph.Env.detect_linux()


def _build(game, env, tags, out_path, steam_index: SteamIndex | None = None):
    """Build a bundle, anchoring <base>/<root>/<game> to the real Steam install
    dir when the game is installed, and capturing registry keys on Windows."""
    root = install_dir = None
    if steam_index is not None:
        ig = steam_index.get(game.steam_id)
        if ig is not None:
            root, install_dir = ig.root, ig.install_name
    registry = []
    if winregistry.available():
        want = set(tags)
        keys = [r.key for r in game.registry if (r.tags & want)]
        if keys:
            registry = winregistry.capture_keys(keys)
    return build_game_bundle(game, env, tags, out_path, root=root,
                             install_dir=install_dir, registry=registry)


def scan_games(manifest: Manifest, env: ph.Env, tags: Iterable[str] = DEFAULT_TAGS,
               only: list[str] | None = None) -> list:
    """Dry run: return BundleResult-like previews without uploading.

    Builds bundles to a temp dir to discover what actually exists on disk, then
    discards the archives (keeps only the mappings/counts).
    """
    results = []
    names = only or list(manifest.games.keys())
    steam_index = SteamIndex.build()
    with tempfile.TemporaryDirectory() as tmp:
        for name in names:
            if name not in manifest:
                continue
            out = Path(tmp) / f"{_safe(name)}.tar.gz"
            res = _build(manifest[name], env, tags, out, steam_index)
            if res:
                results.append(res)
    return results


def run_backup(api: ApiClient, manifest: Manifest, env: ph.Env,
               tags: Iterable[str] = DEFAULT_TAGS, only: list[str] | None = None,
               dry_run: bool = False, progress=None) -> list[dict]:
    """Back up matching games and upload each. Returns a per-game report.

    ``progress`` is an optional callback ``(index, total, game_name)`` invoked
    once per game scanned (useful for a progress bar over the full manifest)."""
    report: list[dict] = []
    names = only or list(manifest.games.keys())
    total = len(names)
    steam_index = SteamIndex.build()
    with tempfile.TemporaryDirectory() as tmp:
        for i, name in enumerate(names, 1):
            if progress:
                progress(i, total, name)
            if name not in manifest:
                report.append({"game": name, "status": "unknown-game"})
                continue
            out = Path(tmp) / f"{_safe(name)}.tar.gz"
            res = _build(manifest[name], env, tags, out, steam_index)
            if not res:
                continue  # not installed / no saves here
            reg = len(res.mapping.get("registry", []))
            if dry_run:
                report.append({
                    "game": name, "status": "found",
                    "files": res.file_count, "bytes": res.total_bytes, "registry": reg,
                })
                continue
            # Keep the server-side mapping small (no registry payload in headers).
            server_mapping = {k: v for k, v in res.mapping.items() if k != "registry"}
            resp = api.upload(name, out, source_os=env.os, mapping=server_mapping)
            report.append({
                "game": name, "status": "uploaded",
                "version": resp.get("version"),
                "files": res.file_count, "bytes": res.total_bytes, "registry": reg,
            })
    return report


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip()
