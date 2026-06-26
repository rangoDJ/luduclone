"""Self-update for the packaged client executables.

Checks the latest GitHub release, and (for a frozen PyInstaller exe) downloads
the matching asset and swaps it in. Running from source can't self-replace, so
we just report that an update exists.

Self-replacement strategy:
  * Windows: a running .exe can be renamed but not overwritten, so we move the
    current exe aside to ``*.old``, then move the downloaded file into place.
    The stale ``*.old`` is removed on the next launch (``cleanup_old``).
  * Linux/macOS: the running file's inode stays valid, so ``os.replace`` over it
    works directly.
"""
from __future__ import annotations

import dataclasses
import os
import re
import sys
import tempfile
from pathlib import Path

import requests

from .version import __version__

GITHUB_REPO = "rangoDJ/luduclone"
API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"


@dataclasses.dataclass
class Release:
    version: str            # numeric, e.g. "0.1.4"
    tag: str                # e.g. "v0.1.4"
    assets: dict[str, str]  # asset name -> browser_download_url
    html_url: str


def is_frozen() -> bool:
    """True when running as a PyInstaller bundle (self-update is possible)."""
    return bool(getattr(sys, "frozen", False))


def current_version() -> str:
    return __version__


def asset_name() -> str | None:
    """Name of the exe asset matching the running executable, or None if not
    frozen (so we can't know which exe to download)."""
    if not is_frozen():
        return None
    return Path(sys.executable).name


def _parse(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def fetch_latest(timeout: int = 15) -> Release | None:
    r = requests.get(
        API_LATEST,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "luduclone"},
        timeout=timeout,
    )
    r.raise_for_status()
    d = r.json()
    tag = d.get("tag_name", "")
    if not tag:
        return None
    assets = {a["name"]: a["browser_download_url"] for a in d.get("assets", [])}
    return Release(version=tag.lstrip("vV"), tag=tag, assets=assets,
                   html_url=d.get("html_url", ""))


def update_available(rel: Release | None = None) -> Release | None:
    """Return the release if it is newer than the running version, else None."""
    rel = rel or fetch_latest()
    if rel is None:
        return None
    return rel if _parse(rel.version) > _parse(__version__) else None


def apply_update(rel: Release, progress=None) -> Path:
    """Download the matching asset and swap it in. Returns the exe path.

    Raises if running from source or the release lacks a matching asset.
    """
    if not is_frozen():
        raise RuntimeError(
            "Auto-update only works for the packaged executable. "
            "Running from source: use 'git pull' instead.")
    exe = Path(sys.executable)
    name = asset_name()
    url = rel.assets.get(name)
    if not url:
        raise RuntimeError(f"Release {rel.tag} has no asset named {name!r} "
                           f"(available: {', '.join(rel.assets) or 'none'})")

    tmp = exe.with_name(exe.name + ".new")
    _download(url, tmp, progress)

    if os.name == "nt":
        old = exe.with_name(exe.name + ".old")
        _silent_unlink(old)
        os.replace(exe, old)   # move running exe aside (allowed on Windows)
        os.replace(tmp, exe)   # put the new one in place
    else:
        os.replace(tmp, exe)
        os.chmod(exe, 0o755)
    return exe


def cleanup_old() -> None:
    """Remove a leftover ``*.old`` from a previous self-update. Best-effort."""
    if not is_frozen():
        return
    _silent_unlink(Path(sys.executable).with_name(Path(sys.executable).name + ".old"))


def _download(url: str, dest: Path, progress=None) -> None:
    with requests.get(url, stream=True, timeout=120,
                      headers={"User-Agent": "luduclone"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)


def _silent_unlink(p: Path) -> None:
    try:
        if p.exists():
            p.unlink()
    except OSError:
        pass
