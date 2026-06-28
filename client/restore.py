"""Restore flow.

Two targeting modes, chosen automatically:

* **proton** -- the game has a Steam Proton prefix on this device, so we re-root
  the original Windows save templates into ``compatdata/<appid>/pfx`` and also
  merge any captured registry keys into the prefix's ``user.reg``/``system.reg``.
* **native** -- the game has native Linux save locations in the manifest (an
  official Linux build); we map each backed-up entry onto the matching Linux
  template and extract there.

``auto`` prefers proton when a prefix exists (the Steam Deck case), else native.
"""
from __future__ import annotations

import dataclasses
import glob
import os
import re
import tempfile
from pathlib import Path

from shared import placeholders as ph
from shared import registry as reg
from shared.manifest import Manifest, Game
from shared.scan import _literal_prefix, _is_absolute as _is_anchored

from .api import ApiClient
from . import bundle
from . import steam
from .roots import SteamIndex, InstalledGame

_FIRST_TOKEN = re.compile(r"<[a-zA-Z]+>")


@dataclasses.dataclass
class EntryOutcome:
    template: str
    target: str | None
    files: int
    status: str   # restored | skipped-wildcard | skipped-no-files | skipped-unmatched
    # Per-file breakdown (filled on restore and on preview).
    new: int = 0
    changed: int = 0
    identical: int = 0
    # In preview mode only: [{"rel", "status", "size"}] for each bundled file.
    file_diffs: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class RestoreResult:
    game: str
    status: str                 # restored | no-data | no-target | not-on-server | error
    mode: str | None = None
    target_root: str | None = None
    entries: list[EntryOutcome] = dataclasses.field(default_factory=list)
    registry_files: list[str] = dataclasses.field(default_factory=list)
    detail: str | None = None
    # Where overwritten files were stashed, so a restore can be reverted.
    undo_dir: str | None = None


def restore_game(api: ApiClient, manifest: Manifest, game_name: str, *,
                 version: int | None = None, mode: str = "auto",
                 dry_run: bool = False, preview: bool = False,
                 do_registry: bool = True, redirects=None,
                 steam_index: SteamIndex | None = None) -> RestoreResult:
    """Download a game's bundle and write it to the right place for this device.

    ``dry_run`` resolves targets without touching disk or reading existing files.
    ``preview`` goes further: it compares each bundled file against what's already
    there (new / changed / identical) so the caller can show a diff before
    overwriting -- still without writing anything. Neither writes; a real restore
    skips identical files and stashes overwritten ones in an undo dir.
    """
    if game_name not in manifest:
        return RestoreResult(game_name, "error", detail="not in manifest")
    game = manifest[game_name]
    if steam_index is None:
        steam_index = SteamIndex.build()
    if redirects is None:
        from .custom import CustomConfig
        redirects = CustomConfig.load().redirects
    installed = steam_index.get(game.steam_id)
    rkw = _resolve_args(game, installed)

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "bundle.tar.gz"
        try:
            header = api.download_latest(game_name, dest, version=version)
        except Exception as e:  # noqa: BLE001
            return RestoreResult(game_name, "not-on-server", detail=str(e))

        meta = bundle.read_bundle_meta(dest) or header.get("mapping", {})
        entries = meta.get("entries", [])
        reg_keys = [reg.RegKey.from_dict(d) for d in meta.get("registry", [])]

        chosen, target = _decide_mode(game, mode, installed)
        if chosen is None:
            return RestoreResult(game_name, "no-target", detail=target)

        undo_dir = None if (dry_run or preview) else _new_undo_dir(game_name)
        if chosen == "proton":
            res = _restore_proton(game, dest, entries, reg_keys, Path(target),
                                  rkw, undo_dir, redirects, dry_run=dry_run,
                                  preview=preview, do_registry=do_registry)
        elif chosen == "windows":
            res = _restore_windows(game, dest, entries, rkw, undo_dir, redirects,
                                   dry_run=dry_run, preview=preview)
        else:
            res = _restore_native(game, dest, entries, rkw, undo_dir, redirects,
                                  dry_run=dry_run, preview=preview)
        if undo_dir is not None and undo_dir.exists():
            res.undo_dir = str(undo_dir)
        return res


def _decide_mode(game: Game, mode: str,
                 installed: InstalledGame | None) -> tuple[str | None, str]:
    """Return (mode, target). For proton, target is the pfx path; otherwise a
    human-readable note when no target is available."""
    on_windows = os.name == "nt"
    pfx = installed.prefix if installed and installed.prefix else (
        steam.compat_prefix(game.steam_id) if game.steam_id is not None else None)
    has_native = bool(game.save_files("linux"))
    has_windows = bool(game.save_files("windows"))

    if mode == "windows":
        return ("windows", "windows") if has_windows else (
            None, "manifest has no Windows save location")
    if mode == "proton":
        if pfx is None:
            return None, f"no Proton prefix for appid {game.steam_id}"
        return "proton", str(pfx)
    if mode == "native":
        if not has_native:
            return None, "manifest has no native Linux save location"
        return "native", "native"
    # auto: restoring on Windows writes back to Windows locations; on Linux,
    # prefer a Proton prefix, else native paths.
    if on_windows and has_windows:
        return "windows", "windows"
    if pfx is not None:
        return "proton", str(pfx)
    if has_native:
        return "native", "native"
    return None, ("not installed via Proton here and no native save path for "
                  "this OS in the manifest")


def _restore_windows(game: Game, bundle_path: Path, entries, rkw: dict,
                     undo_dir: Path | None, redirects, *, dry_run: bool,
                     preview: bool) -> RestoreResult:
    """Restore back to the original Windows locations (client running on Windows)."""
    env = ph.Env.detect_windows()
    outcomes = [_extract_to_template(bundle_path, e, e["template"], env, rkw,
                                     undo_dir, redirects, dry_run=dry_run, preview=preview)
                for e in entries]
    return RestoreResult(game.name, _roll_up(outcomes), mode="windows",
                         target_root=str(env.home), entries=outcomes)


def _resolve_args(game: Game, installed: InstalledGame | None) -> dict:
    """Resolver kwargs for restore: anchor <base>/<root>/<game> to the real
    install dir and supply <storeGameId> (the Steam app id) when known.

    ``<storeUserId>`` is deliberately left unset so it stays a wildcard and is
    glob-matched against the real on-disk folder -- games name that folder with
    either the 32-bit account id or the 17-digit SteamID64, so pinning it to the
    local account id would target (or create) the wrong directory."""
    kw: dict = {}
    if installed is not None:
        kw["base"] = installed.base
        kw["root"] = installed.root
        kw["game_install_dir"] = installed.install_name
    if game.steam_id is not None:
        kw["store_game_id"] = str(game.steam_id)
    return kw


def _restore_proton(game: Game, bundle_path: Path, entries, reg_keys, pfx: Path,
                    rkw: dict, undo_dir: Path | None, redirects, *, dry_run: bool,
                    preview: bool, do_registry: bool) -> RestoreResult:
    # Windows placeholders resolve into the prefix; <base>/<root>/<game> resolve
    # into the game's real install dir on this device.
    env = ph.Env.for_proton_prefix(pfx)
    outcomes = [_extract_to_template(bundle_path, e, e["template"], env, rkw,
                                     undo_dir, redirects, dry_run=dry_run, preview=preview)
                for e in entries]

    reg_files: list[str] = []
    if do_registry and reg_keys:
        if dry_run or preview:
            reg_files = [f"(would merge {len(reg_keys)} keys into user/system.reg)"]
        else:
            reg_files = reg.apply_to_prefix(reg_keys, pfx)

    status = _roll_up(outcomes, extra=bool(reg_files))
    return RestoreResult(game.name, status, mode="proton", target_root=str(pfx),
                         entries=outcomes, registry_files=reg_files)


def _restore_native(game: Game, bundle_path: Path, entries, rkw: dict,
                    undo_dir: Path | None, redirects, *, dry_run: bool,
                    preview: bool) -> RestoreResult:
    env = ph.Env.detect_linux()
    linux_entries = game.save_files("linux")
    outcomes: list[EntryOutcome] = []
    for e in entries:
        target_tmpl = _match_linux_template(e["template"], linux_entries)
        if target_tmpl is None:
            outcomes.append(EntryOutcome(e["template"], None, len(e.get("files", [])),
                                         "skipped-unmatched"))
            continue
        outcomes.append(_extract_to_template(bundle_path, e, target_tmpl, env, rkw,
                                             undo_dir, redirects, dry_run=dry_run,
                                             preview=preview))
    return RestoreResult(game.name, _roll_up(outcomes), mode="native",
                         target_root=str(env.home), entries=outcomes)


def _roll_up(outcomes, extra: bool = False) -> str:
    return "restored" if (extra or any(o.status == "restored" for o in outcomes)) else "no-data"


def _match_linux_template(source_template: str, linux_entries) -> str | None:
    """Pick the Linux manifest template corresponding to a source entry.

    Match by the path suffix after the leading placeholder (games typically keep
    the same trailing subpath across OSes). If there's exactly one Linux entry,
    use it unconditionally."""
    if len(linux_entries) == 1:
        return linux_entries[0].template
    src_suffix = _suffix(source_template)
    for le in linux_entries:
        if _suffix(le.template) == src_suffix:
            return le.template
    return None


def _suffix(template: str) -> str:
    return _FIRST_TOKEN.sub("", template, count=1)


def _extract_to_template(bundle_path: Path, entry: dict, template: str,
                         env: ph.Env, rkw: dict, undo_dir: Path | None,
                         redirects, *, dry_run: bool, preview: bool) -> EntryOutcome:
    """Resolve ``template`` in ``env`` and extract (or preview) the entry's files."""
    from .custom import apply_redirects
    resolved = ph.resolve(template, env, **rkw)
    base = _literal_prefix(resolved)
    nfiles = len(entry.get("files", []))
    if not _is_anchored(base):
        # An unresolved <base>/<root> (game not installed here) -> can't target.
        return EntryOutcome(entry["template"], None, nfiles, "skipped-unmatched")
    if "*" in base:
        candidates = [Path(p) for p in glob.glob(base)]
        if len(candidates) == 1:
            base = str(candidates[0])
        else:
            return EntryOutcome(entry["template"], None, nfiles, "skipped-wildcard")
    base = apply_redirects(base, redirects)
    target = Path(base)
    if preview:
        diffs = bundle.diff_entry_files(bundle_path, entry["index"], target)
        return EntryOutcome(entry["template"], str(target), nfiles, "restored",
                            new=_count(diffs, "new"), changed=_count(diffs, "changed"),
                            identical=_count(diffs, "identical"), file_diffs=diffs)
    if dry_run:
        return EntryOutcome(entry["template"], str(target), nfiles, "restored")
    results = bundle.extract_entry_files(bundle_path, entry["index"], target,
                                         undo_dir=undo_dir)
    new, changed, ident = (_count(results, k) for k in ("new", "changed", "identical"))
    status = "restored" if results else "skipped-no-files"
    return EntryOutcome(entry["template"], str(target), new + changed, status,
                        new=new, changed=changed, identical=ident)


def _count(items: list[dict], status: str) -> int:
    return sum(1 for it in items if it["status"] == status)


def _new_undo_dir(game_name: str) -> Path:
    """A fresh, timestamped directory to stash overwritten files for this restore.
    Created lazily (only when a file is actually overwritten)."""
    import time
    from .config import CONFIG_PATH
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return CONFIG_PATH.parent / "undo" / _slug(game_name) / stamp


def _slug(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._- " else "_" for c in name).strip() or "_"
