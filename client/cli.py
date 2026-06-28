"""luduclone client CLI.

Examples:
    # one-time setup (stored in the user config dir)
    python -m client configure --server http://nas:8000 --token secret

    # see what would be backed up on this machine (no upload)
    python -m client scan
    python -m client scan --game "Celeste" --game "Hollow Knight"

    # back up everything found and upload it
    python -m client backup

    # list games that have backups on the server
    python -m client remote
"""
from __future__ import annotations

import argparse
import sys

from shared import manifest as manifest_mod

from .api import ApiClient
from .backup import detect_env, run_backup
from .config import ClientConfig
from .custom import CustomConfig, CUSTOM_PATH
from .restore import restore_game
from .version import __version__
from . import updater


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}B"


def _load(cfg, force_remote=False):
    """Load the manifest from local cache (else fetch), then layer the user's
    custom games on top so they're scannable/restorable like any other game."""
    api = ApiClient(cfg)
    if force_remote or not cfg.manifest_cache.exists():
        api.fetch_manifest()
    manifest = manifest_mod.Manifest.from_yaml(
        cfg.manifest_cache.read_text(encoding="utf-8")
    )
    CustomConfig.load().merge_into(manifest)
    return api, manifest


def cmd_configure(args) -> int:
    cfg = ClientConfig(server=args.server.rstrip("/"), token=args.token,
                       retain=args.retain or 0)
    cfg.save()
    api = ApiClient(cfg)
    try:
        health = api.health()
    except Exception as e:  # noqa: BLE001
        print(f"Saved config, but server check failed: {e}", file=sys.stderr)
        return 1
    print(f"Configured. Server {cfg.server} is {health}.")
    return 0


def cmd_scan(args) -> int:
    cfg = ClientConfig.load(args.server, args.token)
    api, manifest = _load(cfg, force_remote=args.refresh)
    env = detect_env()
    tags = _tags(args)
    report = run_backup(api, manifest, env, tags=tags, only=args.game or None, dry_run=True)
    if not report:
        print(f"No save data found on this {env.os} machine "
              f"(scanned {len(manifest)} games).")
        return 0
    print(f"Found saves for {len(report)} game(s) on {env.os}:")
    for r in sorted(report, key=lambda r: r["game"]):
        reg = f"  +{r['registry']} reg" if r.get("registry") else ""
        print(f"  {r['game']:<40} {r['files']:>4} files  {_human(r['bytes'])}{reg}")
    return 0


def cmd_backup(args) -> int:
    cfg = ClientConfig.load(args.server, args.token)
    api, manifest = _load(cfg, force_remote=args.refresh)
    env = detect_env()
    tags = _tags(args)
    report = run_backup(api, manifest, env, tags=tags, only=args.game or None)
    uploaded = [r for r in report if r["status"] == "uploaded"]
    for r in sorted(uploaded, key=lambda r: r["game"]):
        reg = f"  +{r['registry']} reg" if r.get("registry") else ""
        pruned = f"  (pruned {len(r['pruned'])} old)" if r.get("pruned") else ""
        print(f"  uploaded {r['game']:<38} v{r['version']}  "
              f"{r['files']} files  {_human(r['bytes'])}{reg}{pruned}")
    print(f"Done. Uploaded {len(uploaded)} game(s) from {env.os}.")
    return 0


def cmd_restore(args) -> int:
    cfg = ClientConfig.load(args.server, args.token)
    api, manifest = _load(cfg, force_remote=args.refresh)
    # Determine which games to restore: explicit list, else everything on server.
    if args.game:
        names = args.game
    else:
        names = [g["game"] for g in api.list_games()]
    if not names:
        print("Nothing to restore (no --game and no server backups).")
        return 0
    from .roots import SteamIndex
    index = SteamIndex.build()
    any_fail = False
    for name in names:
        res = restore_game(api, manifest, name, version=args.version, mode=args.mode,
                           dry_run=args.dry_run, preview=args.preview,
                           do_registry=not args.no_registry, steam_index=index)
        mode = f" via {res.mode}" if res.mode else ""
        print(f"{name}: {res.status}{mode}" + (f" ({res.detail})" if res.detail else ""))
        if res.target_root:
            print(f"    target: {res.target_root}")
        for o in res.entries:
            counts = _fmt_counts(o)
            print(f"    [{o.status}] {o.template} -> {o.target} ({o.files} files{counts})")
            if args.preview:
                for d in o.file_diffs:
                    if d["status"] != "identical":
                        print(f"        {d['status']:<9} {d['rel']} ({_human(d['size'])})")
        for rf in res.registry_files:
            print(f"    [registry] {rf}")
        if res.undo_dir:
            print(f"    undo: {res.undo_dir}")
        if res.status in ("error", "no-target", "not-on-server"):
            any_fail = True
    return 1 if any_fail else 0


def _fmt_counts(o) -> str:
    """Render the new/changed/identical breakdown of one restored entry."""
    parts = []
    if o.new:
        parts.append(f"{o.new} new")
    if o.changed:
        parts.append(f"{o.changed} changed")
    if o.identical:
        parts.append(f"{o.identical} unchanged")
    return f"; {', '.join(parts)}" if parts else ""


def cmd_update(args) -> int:
    try:
        rel = updater.fetch_latest()
    except Exception as e:  # noqa: BLE001
        print(f"Update check failed: {e}", file=sys.stderr)
        return 1
    if rel is None:
        print("Could not determine the latest release.", file=sys.stderr)
        return 1
    print(f"Current: v{updater.current_version()}   Latest: {rel.tag}")
    if updater.update_available(rel) is None:
        print("You are up to date.")
        return 0
    if not updater.is_frozen():
        print(f"Update {rel.tag} available. Running from source — `git pull` to update.")
        print(f"Release notes: {rel.html_url}")
        return 0
    if not args.apply:
        print(f"Update {rel.tag} available. Re-run with --apply to install.")
        return 0
    print(f"Downloading {rel.tag}…")
    last = [-1]

    def prog(done, total):
        pct = int(100 * done / total) if total else 0
        if pct != last[0] and pct % 10 == 0:
            last[0] = pct
            print(f"  {pct}%")

    exe = updater.apply_update(rel, progress=prog)
    print(f"Updated to {rel.tag}. Restart {exe.name} to use the new version.")
    return 0


def cmd_forget(args) -> int:
    cfg = ClientConfig.load(args.server, args.token)
    api = ApiClient(cfg)
    rc = 0
    for name in args.game:
        try:
            resp = api.delete_game(name)
            print(f"  forgot {name} ({resp['deleted_versions']} version(s))")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: {e}", file=sys.stderr)
            rc = 1
    return rc


def cmd_prefixes(args) -> int:
    from . import steam
    from .roots import SteamIndex
    libs = steam.library_dirs()
    print(f"Steam libraries ({len(libs)}):")
    for lib in libs:
        print(f"  {lib}")
    index = SteamIndex.build()
    print(f"\nInstalled Steam games ({len(index)}):")
    for ig in sorted(index.by_appid.values(), key=lambda g: g.name.lower()):
        tag = "  [proton]" if ig.prefix else ""
        print(f"  {ig.appid:<10} {ig.name}{tag}")
        print(f"             install: {ig.install_dir}")
    return 0


def cmd_remote(args) -> int:
    cfg = ClientConfig.load(args.server, args.token)
    api = ApiClient(cfg)
    games = api.list_games()
    if not games:
        print("No backups on the server yet.")
        return 0
    print(f"{len(games)} game(s) backed up on the server:")
    for g in games:
        print(f"  {g['game']:<40} {g['versions']} version(s), latest v{g['latest']}")
    return 0


def _tags(args):
    tags = {"save"}
    if args.config:
        tags.add("config")
    return tags


# ---- custom games / redirects / ignores ------------------------------------
def cmd_custom_list(args) -> int:
    cc = CustomConfig.load()
    print(f"Custom games ({len(cc.games)}):")
    for g in cc.games:
        sid = f", steam {g['steam_id']}" if g.get("steam_id") else ""
        print(f"  {g.get('name')}  ({len(g.get('files') or [])} path(s){sid})")
        for p in g.get("files") or []:
            print(f"      {p}")
    print(f"Redirects ({len(cc.redirects)}):")
    for i, r in enumerate(cc.redirects):
        print(f"  [{i}] {r.get('source')}  ->  {r.get('target')}")
    print(f"Ignores ({len(cc.ignores)}):")
    for i, pat in enumerate(cc.ignores):
        print(f"  [{i}] {pat}")
    print(f"\n(stored in {CUSTOM_PATH})")
    return 0


def cmd_custom_add_game(args) -> int:
    cc = CustomConfig.load()
    cc.games = [g for g in cc.games if g.get("name") != args.name]
    cc.games.append({"name": args.name, "files": args.path or [],
                     "registry": args.registry or [], "steam_id": args.steam_id})
    cc.save()
    print(f"Saved custom game {args.name!r} ({len(args.path or [])} path(s)).")
    return 0


def cmd_custom_rm_game(args) -> int:
    cc = CustomConfig.load()
    before = len(cc.games)
    cc.games = [g for g in cc.games if g.get("name") != args.name]
    cc.save()
    if len(cc.games) == before:
        print(f"No custom game named {args.name!r}.", file=sys.stderr)
        return 1
    print(f"Removed custom game {args.name!r}.")
    return 0


def cmd_custom_add_redirect(args) -> int:
    cc = CustomConfig.load()
    cc.redirects.append({"source": args.source, "target": args.target})
    cc.save()
    print(f"Added redirect {args.source} -> {args.target}.")
    return 0


def cmd_custom_add_ignore(args) -> int:
    cc = CustomConfig.load()
    cc.ignores.append(args.pattern)
    cc.save()
    print(f"Added ignore pattern {args.pattern!r}.")
    return 0


def cmd_custom_rm(args) -> int:
    cc = CustomConfig.load()
    lst = cc.redirects if args.what == "redirect" else cc.ignores
    if not (0 <= args.index < len(lst)):
        print(f"Index {args.index} out of range (have {len(lst)}).", file=sys.stderr)
        return 1
    lst.pop(args.index)
    cc.save()
    print(f"Removed {args.what} [{args.index}].")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="luduclone", description="Cross-OS game-save sync client")
    p.add_argument("--server", help="Server base URL (overrides config/env)")
    p.add_argument("--token", help="Auth token (overrides config/env)")
    p.add_argument("-V", "--version", action="version", version=f"luduclone {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("configure", help="Save server URL + token")
    c.add_argument("--server", required=True)
    c.add_argument("--token")
    c.add_argument("--retain", type=int, default=0,
                   help="Keep at most this many backup versions per game (0 = unlimited)")
    c.set_defaults(func=cmd_configure)

    for name, func, help_ in (
        ("scan", cmd_scan, "Preview what would be backed up (no upload)"),
        ("backup", cmd_backup, "Back up found saves and upload them"),
    ):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--game", action="append", help="Limit to this game (repeatable)")
        s.add_argument("--config", action="store_true", help="Include config files, not just saves")
        s.add_argument("--refresh", action="store_true", help="Refetch manifest from server")
        s.set_defaults(func=func)

    re_ = sub.add_parser("restore", help="Download saves and inject into Proton prefixes")
    re_.add_argument("--game", action="append", help="Limit to this game (repeatable)")
    re_.add_argument("--version", type=int, help="Restore a specific version (default: latest)")
    re_.add_argument("--mode", choices=("auto", "proton", "native", "windows"),
                     default="auto", help="Target selection (default: auto)")
    re_.add_argument("--no-registry", action="store_true",
                     help="Skip merging registry keys into the Proton prefix")
    re_.add_argument("--dry-run", action="store_true", help="Show targets without writing")
    re_.add_argument("--preview", action="store_true",
                     help="Show a per-file diff (new/changed/unchanged) without writing")
    re_.add_argument("--refresh", action="store_true", help="Refetch manifest from server")
    re_.set_defaults(func=cmd_restore)

    px = sub.add_parser("prefixes", help="List discovered Steam libraries + Proton prefixes")
    px.set_defaults(func=cmd_prefixes)

    r = sub.add_parser("remote", help="List games backed up on the server")
    r.set_defaults(func=cmd_remote)

    fg = sub.add_parser("forget", help="Delete a game's backups from the server")
    fg.add_argument("game", nargs="+", help="Game name(s) to delete")
    fg.set_defaults(func=cmd_forget)

    up = sub.add_parser("update", help="Check for and install client updates")
    up.add_argument("--apply", action="store_true", help="Download and install if newer")
    up.set_defaults(func=cmd_update)

    cu = sub.add_parser("custom", help="Manage custom games, redirects, ignore filters")
    cusub = cu.add_subparsers(dest="custom_cmd", required=True)
    cusub.add_parser("list", help="Show the custom config").set_defaults(func=cmd_custom_list)
    ag = cusub.add_parser("add-game", help="Add or replace a custom game")
    ag.add_argument("name")
    ag.add_argument("--path", action="append",
                    help="Save path/template, e.g. '<home>/.mygame' (repeatable)")
    ag.add_argument("--registry", action="append", help="Registry key (repeatable)")
    ag.add_argument("--steam-id", type=int, dest="steam_id",
                    help="Steam app id, to anchor <base>/<root> on restore")
    ag.set_defaults(func=cmd_custom_add_game)
    rg = cusub.add_parser("rm-game", help="Remove a custom game")
    rg.add_argument("name")
    rg.set_defaults(func=cmd_custom_rm_game)
    ar = cusub.add_parser("add-redirect", help="Add a restore path redirect")
    ar.add_argument("source")
    ar.add_argument("target")
    ar.set_defaults(func=cmd_custom_add_redirect)
    ai = cusub.add_parser("add-ignore", help="Add a backup ignore glob")
    ai.add_argument("pattern")
    ai.set_defaults(func=cmd_custom_add_ignore)
    rr = cusub.add_parser("rm-redirect", help="Remove a redirect by index")
    rr.add_argument("index", type=int)
    rr.set_defaults(func=cmd_custom_rm, what="redirect")
    ri = cusub.add_parser("rm-ignore", help="Remove an ignore by index")
    ri.add_argument("index", type=int)
    ri.set_defaults(func=cmd_custom_rm, what="ignore")
    return p


def main(argv=None) -> int:
    updater.cleanup_old()  # remove any leftover *.old from a prior self-update
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
