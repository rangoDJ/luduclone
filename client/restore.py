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


@dataclasses.dataclass
class RestoreResult:
    game: str
    status: str                 # restored | no-data | no-target | not-on-server | error
    mode: str | None = None
    target_root: str | None = None
    entries: list[EntryOutcome] = dataclasses.field(default_factory=list)
    registry_files: list[str] = dataclasses.field(default_factory=list)
    detail: str | None = None


def restore_game(api: ApiClient, manifest: Manifest, game_name: str, *,
                 version: int | None = None, mode: str = "auto",
                 dry_run: bool = False, do_registry: bool = True,
                 steam_index: SteamIndex | None = None) -> RestoreResult:
    if game_name not in manifest:
        return RestoreResult(game_name, "error", detail="not in manifest")
    game = manifest[game_name]
    if steam_index is None:
        steam_index = SteamIndex.build()
    installed = steam_index.get(game.steam_id)

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

        if chosen == "proton":
            return _restore_proton(game, dest, entries, reg_keys, Path(target),
                                   installed, dry_run=dry_run, do_registry=do_registry)
        return _restore_native(game, dest, entries, installed, dry_run=dry_run)


def _decide_mode(game: Game, mode: str,
                 installed: InstalledGame | None) -> tuple[str | None, str]:
    """Return (mode, target). For proton, target is the pfx path; otherwise a
    human-readable note when no target is available."""
    pfx = installed.prefix if installed and installed.prefix else (
        steam.compat_prefix(game.steam_id) if game.steam_id is not None else None)
    has_native = bool(game.save_files("linux"))

    if mode == "proton":
        if pfx is None:
            return None, f"no Proton prefix for appid {game.steam_id}"
        return "proton", str(pfx)
    if mode == "native":
        if not has_native:
            return None, "manifest has no native Linux save location"
        return "native", "native"
    # auto
    if pfx is not None:
        return "proton", str(pfx)
    if has_native:
        return "native", "native"
    return None, ("game not installed via Proton here and no native Linux "
                  "save path in manifest")


def _root_args(installed: InstalledGame | None) -> dict:
    """Resolver kwargs that anchor <base>/<root>/<game> to the install dir."""
    if installed is None:
        return {}
    return {"root": installed.root, "game_install_dir": installed.install_name}


def _restore_proton(game: Game, bundle_path: Path, entries, reg_keys, pfx: Path,
                    installed: InstalledGame | None, *, dry_run: bool,
                    do_registry: bool) -> RestoreResult:
    # Windows placeholders resolve into the prefix; <base>/<root>/<game> resolve
    # into the game's real install dir on this device.
    env = ph.Env.for_proton_prefix(pfx)
    rkw = _root_args(installed)
    outcomes = [_extract_to_template(bundle_path, e, e["template"], env,
                                     dry_run=dry_run, **rkw)
                for e in entries]

    reg_files: list[str] = []
    if do_registry and reg_keys:
        if dry_run:
            reg_files = [f"(would merge {len(reg_keys)} keys into user/system.reg)"]
        else:
            reg_files = reg.apply_to_prefix(reg_keys, pfx)

    status = "restored" if (any(o.status == "restored" for o in outcomes) or reg_files) else "no-data"
    return RestoreResult(game.name, status, mode="proton", target_root=str(pfx),
                         entries=outcomes, registry_files=reg_files)


def _restore_native(game: Game, bundle_path: Path, entries,
                    installed: InstalledGame | None, *, dry_run: bool) -> RestoreResult:
    env = ph.Env.detect_linux()
    rkw = _root_args(installed)
    linux_entries = game.save_files("linux")
    outcomes: list[EntryOutcome] = []
    for e in entries:
        target_tmpl = _match_linux_template(e["template"], linux_entries)
        if target_tmpl is None:
            outcomes.append(EntryOutcome(e["template"], None, len(e.get("files", [])),
                                         "skipped-unmatched"))
            continue
        outcomes.append(_extract_to_template(bundle_path, e, target_tmpl, env,
                                             dry_run=dry_run, **rkw))
    status = "restored" if any(o.status == "restored" for o in outcomes) else "no-data"
    return RestoreResult(game.name, status, mode="native", target_root=str(env.home),
                         entries=outcomes)


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
                         env: ph.Env, *, dry_run: bool, root: str | None = None,
                         game_install_dir: str | None = None) -> EntryOutcome:
    """Resolve ``template`` in ``env`` and extract the entry's files into it."""
    resolved = ph.resolve(template, env, root=root, game_install_dir=game_install_dir)
    base = _literal_prefix(resolved)
    if not _is_anchored(base):
        # An unresolved <base>/<root> (game not installed here) -> can't target.
        return EntryOutcome(entry["template"], None, len(entry.get("files", [])),
                            "skipped-unmatched")
    if "*" in base:
        candidates = [Path(p) for p in glob.glob(base)]
        if len(candidates) == 1:
            base = str(candidates[0])
        else:
            return EntryOutcome(entry["template"], None, len(entry.get("files", [])),
                                "skipped-wildcard")
    target = Path(base)
    if dry_run:
        return EntryOutcome(entry["template"], str(target), len(entry.get("files", [])),
                            "restored")
    written = bundle.extract_entry_files(bundle_path, entry["index"], target)
    return EntryOutcome(entry["template"], str(target), len(written),
                        "restored" if written else "skipped-no-files")
