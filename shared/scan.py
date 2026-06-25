"""Turn a resolved (globbable) path into concrete files on disk.

A resolved template may contain ``*`` wildcards (from <osUserName>, <storeUserId>,
etc.). We expand those with glob and then walk directories so the caller gets a
flat list of real files, each paired with the path relative to the matched
placeholder root -- that relative path is what we preserve across machines.
"""
from __future__ import annotations

import dataclasses
import glob
import os
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class Match:
    abs_path: Path        # the real file on disk
    base: Path            # the resolved root that matched (the glob's literal prefix)
    rel_path: str         # abs_path relative to base, using forward slashes


def _is_absolute(resolved: str) -> bool:
    """True if the pattern is anchored to an absolute location.

    Handles both OSes regardless of host: a Windows drive path (``C:/...``), a
    POSIX absolute path (``/...``), or a UNC path (``//host/share``). A leading
    wildcard segment (``*/...``) is treated as relative.
    """
    if resolved.startswith("/"):
        return True
    # Drive-letter path like C:/ or C:\
    if len(resolved) >= 3 and resolved[1] == ":" and resolved[2] in "/\\":
        return True
    return False


def _literal_prefix(resolved: str) -> str:
    """Return the leading portion of the pattern before the first wildcard."""
    cut = len(resolved)
    for ch in ("*", "?", "["):
        i = resolved.find(ch)
        if i != -1:
            cut = min(cut, i)
    head = resolved[:cut]
    # Trim back to the last complete path segment.
    if "/" in head:
        head = head[: head.rfind("/")]
    return head


def scan(resolved: str) -> list[Match]:
    """Expand a resolved template to concrete files.

    Directories are walked recursively into their constituent files.

    Patterns that are not absolute paths are ignored. These come from templates
    built on ``<base>``/``<root>`` (the game's install dir) when no game root is
    configured: they collapse to a relative glob like ``*/*/data`` that would
    otherwise match arbitrary folders relative to the process's working
    directory -- the source of "phantom" matches for games that aren't installed.
    """
    if not _is_absolute(resolved):
        return []
    base = Path(_literal_prefix(resolved))
    out: list[Match] = []
    for hit in glob.glob(resolved, recursive=True):
        p = Path(hit)
        if p.is_dir():
            for f in _walk_files(p):
                out.append(_make_match(f, base))
        elif p.is_file():
            out.append(_make_match(p, base))
    return out


def _walk_files(root: Path):
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            yield Path(dirpath) / name


def _make_match(abs_path: Path, base: Path) -> Match:
    try:
        rel = abs_path.relative_to(base).as_posix()
    except ValueError:
        # abs_path not under base (can happen with odd patterns); fall back to name.
        rel = abs_path.name
    return Match(abs_path=abs_path, base=base, rel_path=rel)
