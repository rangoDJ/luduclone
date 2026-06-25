"""Discover Steam libraries and Proton compatdata prefixes on Linux / Steam Deck.

A game run through Proton stores its Windows-style files inside a *compatibility
prefix* at ``<library>/steamapps/compatdata/<appid>/pfx`` (which contains
``drive_c``). We locate that prefix from the game's Steam app id so the restore
flow can re-root Windows save paths into it.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Common Steam root locations (Steam Deck, native, and Flatpak installs).
_STEAM_ROOT_CANDIDATES = (
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)

# Matches `"path"   "/some/library"` lines in libraryfolders.vdf.
_VDF_PATH = re.compile(r'"path"\s*"([^"]+)"')


def steam_roots() -> list[Path]:
    """Return existing Steam root dirs (deduped, symlinks resolved)."""
    seen: dict[Path, None] = {}
    for cand in _STEAM_ROOT_CANDIDATES:
        p = Path(os.path.expanduser(cand))
        if p.exists():
            seen.setdefault(p.resolve(), None)
    return list(seen)


def library_dirs() -> list[Path]:
    """All Steam library folders, including SD-card libraries on a Deck.

    Reads each root's ``steamapps/libraryfolders.vdf``; falls back to the root's
    own ``steamapps`` if the vdf is missing.
    """
    libs: dict[Path, None] = {}
    for root in steam_roots():
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            for m in _VDF_PATH.finditer(vdf.read_text(encoding="utf-8", errors="ignore")):
                lib = Path(m.group(1))
                if lib.exists():
                    libs.setdefault(lib.resolve(), None)
        # The root itself is always a library.
        if (root / "steamapps").exists():
            libs.setdefault(root.resolve(), None)
    return list(libs)


def compat_prefix(appid: int | str) -> Path | None:
    """Return the ``pfx`` dir for a Steam app id, or None if not found."""
    appid = str(appid)
    for lib in library_dirs():
        pfx = lib / "steamapps" / "compatdata" / appid / "pfx"
        if pfx.exists():
            return pfx
    return None


def list_compat_apps() -> dict[str, Path]:
    """Map every installed compatdata app id -> its pfx (for diagnostics)."""
    out: dict[str, Path] = {}
    for lib in library_dirs():
        compat = lib / "steamapps" / "compatdata"
        if not compat.is_dir():
            continue
        for child in compat.iterdir():
            pfx = child / "pfx"
            if pfx.exists():
                out.setdefault(child.name, pfx)
    return out
