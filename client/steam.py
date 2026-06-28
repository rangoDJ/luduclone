"""Discover Steam libraries and Proton compatdata prefixes (Windows + Linux).

A game run through Proton stores its Windows-style files inside a *compatibility
prefix* at ``<library>/steamapps/compatdata/<appid>/pfx`` (which contains
``drive_c``). We locate that prefix from the game's Steam app id so the restore
flow can re-root Windows save paths into it. On Windows we also use the Steam
install to find where each game is installed (for ``<base>`` saves).
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# Common Steam root locations on Linux (Steam Deck, native, Flatpak).
_STEAM_ROOT_CANDIDATES = (
    "~/.steam/steam",
    "~/.steam/root",
    "~/.local/share/Steam",
    "~/.var/app/com.valvesoftware.Steam/.local/share/Steam",
)

# Default Windows Steam install locations (used if the registry lookup fails).
_WINDOWS_STEAM_CANDIDATES = (
    "C:/Program Files (x86)/Steam",
    "C:/Program Files/Steam",
)

# Matches `"path"   "/some/library"` lines in libraryfolders.vdf.
_VDF_PATH = re.compile(r'"path"\s*"([^"]+)"')


def _windows_steam_path() -> Path | None:
    """Read the Steam install path from the Windows registry, if available."""
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                      (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam")):
        try:
            with winreg.OpenKey(hive, key) as k:
                val, _ = winreg.QueryValueEx(k, "SteamPath" if hive ==
                                             winreg.HKEY_CURRENT_USER else "InstallPath")
                p = Path(val)
                if p.exists():
                    return p
        except OSError:
            continue
    return None


def steam_roots() -> list[Path]:
    """Return existing Steam root dirs (deduped, symlinks resolved)."""
    seen: dict[Path, None] = {}
    candidates: list[str] = list(_STEAM_ROOT_CANDIDATES)
    if os.name == "nt":
        candidates += list(_WINDOWS_STEAM_CANDIDATES)
    for cand in candidates:
        p = Path(os.path.expanduser(cand))
        if p.exists():
            seen.setdefault(p.resolve(), None)
    win = _windows_steam_path()
    if win:
        seen.setdefault(win.resolve(), None)
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


def primary_user_id() -> str | None:
    """Best-effort Steam ``<storeUserId>`` (the userdata account id).

    Prefers the most-recent account from ``config/loginusers.vdf``; falls back to
    a sole ``userdata/<id>`` directory. Returns None if it can't be determined.
    """
    # loginusers.vdf: blocks keyed by 17-digit SteamID64 with "MostRecent" "1".
    for root in steam_roots():
        vdf = root / "config" / "loginusers.vdf"
        if not vdf.exists():
            continue
        text = vdf.read_text(encoding="utf-8", errors="ignore")
        # Find each "7656..." block and whether it's MostRecent.
        chosen = None
        for m in re.finditer(r'"(7656\d{13})"\s*\{([^}]*)\}', text, re.DOTALL):
            sid64, body = m.group(1), m.group(2)
            account_id = str(int(sid64) - 76561197960265728)
            if '"MostRecent"' in body and re.search(r'"MostRecent"\s*"1"', body):
                return account_id
            chosen = chosen or account_id
        if chosen:
            return chosen
    # Fallback: a single userdata/<id> directory across libraries/roots.
    ids: set[str] = set()
    for root in steam_roots():
        ud = root / "userdata"
        if ud.is_dir():
            ids.update(c.name for c in ud.iterdir() if c.is_dir() and c.name.isdigit())
    return next(iter(ids)) if len(ids) == 1 else None


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
