"""Local user overrides, mirroring ludusavi's custom games / redirects / ignores.

Stored next to the client config at ``<config dir>/custom.json``:

    {
      "games": [
        {"name": "My Game", "files": ["<home>/.myg/saves"],
         "registry": ["HKEY_CURRENT_USER/Software/MyGame"], "steam_id": null}
      ],
      "redirects": [{"source": "C:/Users/old", "target": "C:/Users/new"}],
      "ignores": ["*/cache/*", "*.tmp"]
    }

* **games**    -- extra games not in (or overriding) the manifest. Each file path
                  is a normal manifest template, so placeholders like ``<home>``
                  or ``<winDocuments>`` work.
* **redirects** -- prefix rewrites applied to a resolved path on **restore** (e.g.
                  a different Windows username or drive on the restore machine).
* **ignores**  -- glob patterns; matching files are skipped during **backup**.
"""
from __future__ import annotations

import dataclasses
import fnmatch
import json
from pathlib import Path

from shared.manifest import Game, FileEntry, RegistryEntry, Manifest

from .config import CONFIG_PATH

CUSTOM_PATH = CONFIG_PATH.parent / "custom.json"


@dataclasses.dataclass
class CustomConfig:
    games: list = dataclasses.field(default_factory=list)
    redirects: list = dataclasses.field(default_factory=list)
    ignores: list = dataclasses.field(default_factory=list)

    @classmethod
    def load(cls, path=None) -> "CustomConfig":
        path = Path(path or CUSTOM_PATH)
        if not path.exists():
            return cls()
        d = json.loads(path.read_text(encoding="utf-8")) or {}
        return cls(games=d.get("games") or [],
                   redirects=d.get("redirects") or [],
                   ignores=d.get("ignores") or [])

    def save(self, path=None) -> None:
        path = Path(path or CUSTOM_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"games": self.games, "redirects": self.redirects,
                        "ignores": self.ignores}, indent=2),
            encoding="utf-8")

    # ---- conversion ----------------------------------------------------
    def to_games(self) -> dict[str, Game]:
        """Turn the custom-game definitions into manifest ``Game`` objects."""
        out: dict[str, Game] = {}
        for g in self.games:
            name = (g or {}).get("name")
            if not name:
                continue
            files = tuple(
                FileEntry(template=p, tags=frozenset({"save"}), constraints=())
                for p in (g.get("files") or []) if p
            )
            registry = tuple(
                RegistryEntry(key=k, tags=frozenset({"config"}))
                for k in (g.get("registry") or []) if k
            )
            out[name] = Game(name=name, files=files, registry=registry,
                             install_dirs=(), steam_id=g.get("steam_id"))
        return out

    def merge_into(self, manifest: Manifest) -> Manifest:
        """Add/override the manifest's games with the custom ones (in place)."""
        manifest.games.update(self.to_games())
        return manifest


def is_ignored(path: str, ignores) -> bool:
    """True if ``path`` matches any ignore glob (case-insensitive, ``/``-normalised)."""
    if not ignores:
        return False
    p = path.replace("\\", "/").lower()
    return any(fnmatch.fnmatch(p, (pat or "").replace("\\", "/").lower())
               for pat in ignores)


def apply_redirects(path: str, redirects) -> str:
    """Rewrite ``path`` by the first matching redirect prefix, else return it
    unchanged. Matching is on whole path segments so ``C:/Users/old`` does not
    accidentally match ``C:/Users/older``."""
    if not redirects:
        return path
    p = path.replace("\\", "/")
    for r in redirects:
        src = (r.get("source") or "").replace("\\", "/").rstrip("/")
        tgt = (r.get("target") or "").replace("\\", "/").rstrip("/")
        if not src or not tgt:
            continue
        if p == src or p.startswith(src + "/"):
            return tgt + p[len(src):]
    return path
