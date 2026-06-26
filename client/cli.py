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
from .restore import restore_game


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}B"


def _load(cfg, force_remote=False):
    """Load the manifest from local cache, else fetch from the server."""
    api = ApiClient(cfg)
    if force_remote or not cfg.manifest_cache.exists():
        api.fetch_manifest()
    return api, manifest_mod.Manifest.from_yaml(
        cfg.manifest_cache.read_text(encoding="utf-8")
    )


def cmd_configure(args) -> int:
    cfg = ClientConfig(server=args.server.rstrip("/"), token=args.token)
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
        print(f"  uploaded {r['game']:<38} v{r['version']}  "
              f"{r['files']} files  {_human(r['bytes'])}{reg}")
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
                           dry_run=args.dry_run, do_registry=not args.no_registry,
                           steam_index=index)
        mode = f" via {res.mode}" if res.mode else ""
        print(f"{name}: {res.status}{mode}" + (f" ({res.detail})" if res.detail else ""))
        if res.target_root:
            print(f"    target: {res.target_root}")
        for o in res.entries:
            print(f"    [{o.status}] {o.template} -> {o.target} ({o.files} files)")
        for rf in res.registry_files:
            print(f"    [registry] {rf}")
        if res.status in ("error", "no-target", "not-on-server"):
            any_fail = True
    return 1 if any_fail else 0


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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="luduclone", description="Cross-OS game-save sync client")
    p.add_argument("--server", help="Server base URL (overrides config/env)")
    p.add_argument("--token", help="Auth token (overrides config/env)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("configure", help="Save server URL + token")
    c.add_argument("--server", required=True)
    c.add_argument("--token")
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
    re_.add_argument("--mode", choices=("auto", "proton", "native"), default="auto",
                     help="Target selection (default: auto)")
    re_.add_argument("--no-registry", action="store_true",
                     help="Skip merging registry keys into the Proton prefix")
    re_.add_argument("--dry-run", action="store_true", help="Show targets without writing")
    re_.add_argument("--refresh", action="store_true", help="Refetch manifest from server")
    re_.set_defaults(func=cmd_restore)

    px = sub.add_parser("prefixes", help="List discovered Steam libraries + Proton prefixes")
    px.set_defaults(func=cmd_prefixes)

    r = sub.add_parser("remote", help="List games backed up on the server")
    r.set_defaults(func=cmd_remote)

    fg = sub.add_parser("forget", help="Delete a game's backups from the server")
    fg.add_argument("game", nargs="+", help="Game name(s) to delete")
    fg.set_defaults(func=cmd_forget)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
