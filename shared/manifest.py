"""Fetch, cache, and parse the ludusavi manifest.

The manifest is the upstream ludusavi data file mapping every known game to the
locations of its save/config files. We reuse it verbatim so luduclone benefits
from the community-maintained database.

Schema (per game key):
    files:
      "<placeholder>/sub/path":
        tags: [save, config, ...]
        when:
          - {os: windows|linux|mac|dos, store: steam|epic|...}
    installDir: { "<dir name>": {} }
    registry: { "HKEY_.../...": { tags: [...] } }   # Windows only
    steam: { id: <int> }
"""
from __future__ import annotations

import dataclasses
import time
import urllib.request
from pathlib import Path
from typing import Iterable

import yaml

MANIFEST_URL = (
    "https://raw.githubusercontent.com/mtkennerly/"
    "ludusavi-manifest/master/data/manifest.yaml"
)

# A "save" by default; clients can opt into other tags.
DEFAULT_TAGS = frozenset({"save"})


@dataclasses.dataclass(frozen=True)
class Constraint:
    """A `when` condition gating whether a path applies on this machine."""

    os: str | None = None
    store: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Constraint":
        return cls(os=d.get("os"), store=d.get("store"))


@dataclasses.dataclass(frozen=True)
class FileEntry:
    template: str            # e.g. "<winAppData>/Celeste"
    tags: frozenset[str]
    constraints: tuple[Constraint, ...]

    def applies_on(self, os_name: str) -> bool:
        """True if this entry is relevant for the given OS.

        An entry with no os constraint applies everywhere. An entry with one or
        more constraints applies if ANY constraint matches the os (constraints
        are OR-ed, matching ludusavi semantics).
        """
        if not self.constraints:
            return True
        oses = [c.os for c in self.constraints if c.os is not None]
        if not oses:
            return True
        return os_name in oses


@dataclasses.dataclass(frozen=True)
class RegistryEntry:
    key: str
    tags: frozenset[str]


@dataclasses.dataclass(frozen=True)
class Game:
    name: str
    files: tuple[FileEntry, ...]
    registry: tuple[RegistryEntry, ...]
    install_dirs: tuple[str, ...]
    steam_id: int | None

    def save_files(self, os_name: str, tags: Iterable[str] = DEFAULT_TAGS) -> list[FileEntry]:
        want = set(tags)
        return [
            f for f in self.files
            if f.applies_on(os_name) and (f.tags & want)
        ]


class Manifest:
    def __init__(self, games: dict[str, Game]):
        self.games = games

    def __getitem__(self, name: str) -> Game:
        return self.games[name]

    def __contains__(self, name: str) -> bool:
        return name in self.games

    def __len__(self) -> int:
        return len(self.games)

    @classmethod
    def parse(cls, raw: dict) -> "Manifest":
        games: dict[str, Game] = {}
        for name, body in (raw or {}).items():
            body = body or {}
            files = []
            for tmpl, meta in (body.get("files") or {}).items():
                meta = meta or {}
                constraints = tuple(
                    Constraint.from_dict(w or {}) for w in (meta.get("when") or [])
                )
                files.append(
                    FileEntry(
                        template=tmpl,
                        tags=frozenset(meta.get("tags") or ["save"]),
                        constraints=constraints,
                    )
                )
            registry = tuple(
                RegistryEntry(key=k, tags=frozenset((v or {}).get("tags") or ["config"]))
                for k, v in (body.get("registry") or {}).items()
            )
            install_dirs = tuple((body.get("installDir") or {}).keys())
            steam = body.get("steam") or {}
            steam_id = steam.get("id")
            games[name] = Game(
                name=name,
                files=tuple(files),
                registry=registry,
                install_dirs=install_dirs,
                steam_id=steam_id,
            )
        return cls(games)

    @classmethod
    def from_yaml(cls, text: str) -> "Manifest":
        return cls.parse(yaml.safe_load(text))


def load(cache_path: Path, max_age_seconds: int = 86400, force: bool = False) -> Manifest:
    """Load the manifest, refreshing the on-disk cache if stale.

    Falls back to the cached copy if the network fetch fails.
    """
    cache_path = Path(cache_path)
    fresh = (
        cache_path.exists()
        and not force
        and (time.time() - cache_path.stat().st_mtime) < max_age_seconds
    )
    if not fresh:
        try:
            text = _download(MANIFEST_URL)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(text, encoding="utf-8")
        except Exception:
            if not cache_path.exists():
                raise
    return Manifest.from_yaml(cache_path.read_text(encoding="utf-8"))


def _download(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "luduclone"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (trusted URL)
        return resp.read().decode("utf-8")
