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
import io
import json
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


def build_game_bundle(game: Game, env: ph.Env, tags, out_path: Path,
                      *, root: str | None = None, install_dir: str | None = None,
                      registry=None) -> BundleResult | None:
    """Scan a game's save locations and write a bundle. Returns None if nothing
    matched on disk (no save files AND no captured registry).

    ``root``/``install_dir`` anchor ``<base>``/``<root>``/``<game>`` to a real
    installed location (from the Steam roots index). When ``install_dir`` is not
    given we fall back to the manifest's declared install dir name.
    ``registry`` is an optional list of shared.registry.RegKey captured from the
    Windows registry; it is embedded in the bundle and never affects file scan.
    """
    entries_meta: list[dict] = []
    members: list[tuple[Path, str]] = []  # (abs_path, arcname)
    total = 0

    install_dir = install_dir or (game.install_dirs[0] if game.install_dirs else None)
    for idx, entry in enumerate(game.save_files(env.os, tags)):
        resolved = ph.resolve(entry.template, env, game_install_dir=install_dir, root=root)
        matches = scan_mod.scan(resolved)
        if not matches:
            continue
        files: list[str] = []
        base = matches[0].base
        for m in matches:
            arcname = f"e{idx}/{m.rel_path}"
            members.append((m.abs_path, arcname))
            files.append(m.rel_path)
            total += m.abs_path.stat().st_size
        entries_meta.append({
            "index": idx,
            "template": entry.template,
            "tags": sorted(entry.tags),
            "base": str(base).replace("\\", "/"),
            "files": files,
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
        for abs_path, arcname in members:
            tar.add(abs_path, arcname=arcname)
        _add_bytes(tar, META_MEMBER, json.dumps(mapping, indent=2).encode("utf-8"))

    return BundleResult(game.name, mapping, len(members), total)


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


def extract_entry_files(bundle_path: Path, index: int, dest_dir: Path) -> list[Path]:
    """Extract the files belonging to one entry (``e{index}/``) into ``dest_dir``,
    preserving their relative subpaths. Returns the written paths.

    Bundles come from a server and are therefore untrusted: member names that
    escape ``dest_dir`` (via ``..`` or an absolute path) are rejected so a crafted
    archive can't write outside the intended directory (path traversal / zip-slip).
    """
    prefix = f"e{index}/"
    written: list[Path] = []
    dest_dir = Path(dest_dir)
    with tarfile.open(bundle_path, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(prefix):
                continue
            target = _safe_join(dest_dir, member.name[len(prefix):])
            if target is None:
                continue  # unsafe member name (path traversal) -> skip
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            if src is None:
                continue
            with open(target, "wb") as f:
                f.write(src.read())
            written.append(target)
    return written


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
