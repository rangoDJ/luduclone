"""Build and read save bundles.

A bundle is a ``.tar.gz`` whose members are laid out as ``e{index}/{rel_path}``,
where ``index`` ties each file back to one manifest file-entry. The bundle's
companion ``mapping`` (uploaded alongside, and echoed back on download) records,
per entry, the original template and the resolved base so the *other* OS can
retarget the files instead of trusting raw absolute paths.

mapping schema:
    {
      "game": "Celeste",
      "source_os": "windows",
      "entries": [
        {
          "index": 0,
          "template": "<winAppData>/Celeste",
          "tags": ["save"],
          "base": "C:/Users/rango/AppData/Roaming/Celeste",
          "files": ["save0.celeste", "settings.celeste"]
        }
      ]
    }
"""
from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import io
import json
import os
import tarfile
import time
from pathlib import Path

from shared import placeholders as ph
from shared import scan as scan_mod
from shared.manifest import Game

# Member inside the tar holding the self-describing bundle metadata.
META_MEMBER = "_meta/bundle.json"


@dataclasses.dataclass
class BundleResult:
    game: str
    mapping: dict
    file_count: int
    total_bytes: int
    # Client-side preview detail (never uploaded): per-entry file list with sizes,
    # so the GUI can show an expandable, ludusavi-style file tree.
    #   [{"template": str, "tags": [str], "base": str,
    #     "files": [{"path": rel, "size": int}]}]
    entries: list = dataclasses.field(default_factory=list)


def build_game_bundle(game: Game, env: ph.Env, tags, out_path: Path,
                      *, installed=None, store_user_id: str | None = None,
                      allowed_stores=None, registry=None,
                      ignores=None) -> BundleResult | None:
    """Scan a game's save locations and write a bundle. Returns None if nothing
    matched on disk (no save files AND no captured registry).

    ``installed`` is an optional roots.InstalledGame anchoring ``<base>``/
    ``<root>``/``<game>`` to the real install dir. When absent we try every
    declared ``installDir`` name plus the game title for the ``<game>`` segment
    (ludusavi behaviour). Symlinks are dereferenced and duplicate files are
    stored once. ``registry`` is embedded and never affects the file scan.
    """
    store_game_id = str(game.steam_id) if game.steam_id is not None else None
    # Candidate (game_dir, base, root) triples to try for the <game>/<base> slot.
    if installed is not None:
        candidates = [(installed.install_name, installed.base, installed.root)]
    else:
        names = list(dict.fromkeys([*game.install_dirs, game.name]))
        candidates = [(n, None, None) for n in (names or [None])]

    entries_meta: list[dict] = []
    entries_preview: list[dict] = []       # client-only: per-file sizes for the GUI
    members: list[tuple[Path, str]] = []   # (real_path, arcname)
    seen: set[Path] = set()                # dedup files already captured
    total = 0

    for idx, entry in enumerate(game.save_files(env.os, tags, allowed_stores)):
        # Resolve against each candidate; unique patterns only (templates without
        # <game>/<base> collapse to one).
        patterns = {
            ph.resolve(entry.template, env, game_install_dir=gd, base=base, root=root,
                       store_user_id=store_user_id, store_game_id=store_game_id)
            for (gd, base, root) in candidates
        }
        matches = [m for p in patterns for m in scan_mod.scan(p)]
        if not matches:
            continue
        files: list[str] = []
        preview_files: list[dict] = []
        base_dir = matches[0].base
        for m in matches:
            real = Path(os.path.realpath(m.abs_path))  # dereference symlinks
            if not real.is_file() or real in seen:
                continue
            if _ignored(str(real), ignores) or _ignored(m.rel_path, ignores):
                continue
            seen.add(real)
            size = real.stat().st_size
            members.append((real, f"e{idx}/{m.rel_path}"))
            files.append(m.rel_path)
            preview_files.append({"path": m.rel_path, "size": size})
            total += size
        if not files:
            continue
        base_str = str(base_dir).replace("\\", "/")
        entries_meta.append({
            "index": idx,
            "template": entry.template,
            "tags": sorted(entry.tags),
            "base": base_str,
            "files": files,
        })
        entries_preview.append({
            "template": entry.template,
            "tags": sorted(entry.tags),
            "base": base_str,
            "files": preview_files,
        })

    registry_dicts = [k.to_dict() for k in (registry or [])]
    if not members and not registry_dicts:
        return None

    mapping = {
        "game": game.name,
        "source_os": env.os,
        "entries": entries_meta,
        "registry": registry_dicts,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for real_path, arcname in members:
            tar.add(real_path, arcname=arcname)
        _add_bytes(tar, META_MEMBER, json.dumps(mapping, indent=2).encode("utf-8"))

    return BundleResult(game.name, mapping, len(members), total, entries_preview)


def _ignored(path: str, ignores) -> bool:
    """True if ``path`` matches any ignore glob (case-insensitive)."""
    if not ignores:
        return False
    p = path.replace("\\", "/").lower()
    return any(fnmatch.fnmatch(p, (pat or "").replace("\\", "/").lower())
               for pat in ignores)


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def read_bundle_meta(bundle_path: Path) -> dict | None:
    """Read the embedded ``_meta/bundle.json`` (entries + registry). The
    authoritative source for restore; None if the bundle predates self-describing
    metadata."""
    with tarfile.open(bundle_path, "r:gz") as tar:
        try:
            member = tar.getmember(META_MEMBER)
        except KeyError:
            return None
        src = tar.extractfile(member)
        if src is None:
            return None
        return json.loads(src.read().decode("utf-8"))


def extract_entry_files(bundle_path: Path, index: int, dest_dir: Path,
                        undo_dir: Path | None = None,
                        skip_identical: bool = True) -> list[dict]:
    """Extract the files belonging to one entry (``e{index}/``) into ``dest_dir``,
    preserving their relative subpaths.

    Returns one ``{"path": str, "status": str}`` per bundled file, where status is
    ``new`` (written, no prior file), ``changed`` (written over a differing file)
    or ``identical`` (already matched on disk; left untouched when
    ``skip_identical``). Like ludusavi, identical files are not rewritten so an
    unchanged save keeps its original mtime.

    Bundles come from a server and are therefore untrusted: member names that
    escape ``dest_dir`` (via ``..`` or an absolute path) are rejected so a crafted
    archive can't write outside the intended directory (path traversal / zip-slip).

    If ``undo_dir`` is given, any existing file about to be overwritten is first
    copied there (under its tar member name) so the restore can be reverted.
    """
    import shutil
    prefix = f"e{index}/"
    results: list[dict] = []
    dest_dir = Path(dest_dir)
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            target = _safe_join(dest_dir, member.name[len(prefix):])
            if target is None:
                continue  # unsafe member name (path traversal) -> skip
            src = tar.extractfile(member)
            if src is None:
                continue
            data = src.read()
            status = _file_status(target, data)
            if status == "identical" and skip_identical:
                results.append({"path": str(target), "status": "identical"})
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if undo_dir is not None and target.exists():
                backup = Path(undo_dir) / member.name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, backup)
            with open(target, "wb") as f:
                f.write(data)
            results.append({"path": str(target), "status": status})
    return results


def diff_entry_files(bundle_path: Path, index: int, dest_dir: Path) -> list[dict]:
    """Compare an entry's bundled files against what's already at ``dest_dir``
    without writing anything. Returns ``{"rel", "status", "size"}`` per file,
    status ``new`` | ``changed`` | ``identical``."""
    prefix = f"e{index}/"
    out: list[dict] = []
    dest_dir = Path(dest_dir)
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            rel = member.name[len(prefix):]
            target = _safe_join(dest_dir, rel)
            if target is None:
                continue
            src = tar.extractfile(member)
            data = src.read() if src else b""
            out.append({"rel": rel, "status": _file_status(target, data),
                        "size": len(data)})
    return out


def _file_status(target: Path, data: bytes) -> str:
    """Classify ``data`` against the file currently at ``target``."""
    try:
        if not target.is_file() or target.stat().st_size != len(data):
            return "new" if not target.exists() else "changed"
    except OSError:
        return "changed"
    return "identical" if _sha_file(target) == hashlib.sha256(data).hexdigest() else "changed"


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_join(dest_dir: Path, rel: str) -> Path | None:
    """Join ``rel`` under ``dest_dir``, or None if it would escape the directory."""
    import os
    rel = rel.replace("\\", "/").lstrip("/")
    norm = os.path.normpath(rel)
    if not norm or norm == "." or norm.startswith("..") or os.path.isabs(norm):
        return None
    if ".." in Path(norm).parts:
        return None
    return dest_dir / norm
