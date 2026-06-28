"""Installed-game detection via Steam roots (modeled on ludusavi's roots).

Ludusavi anchors every save search to a "root" (a game library) and enumerates
what is actually installed there before resolving manifest paths. That is what
lets ``<base>``/``<root>``/``<game>`` resolve to a real install directory instead
of a wildcard -- and what prevents "phantom" matches for games you don't own.

This module covers the Steam case (which is the whole library on a Steam Deck):
it parses every ``appmanifest_<appid>.acf`` across all Steam libraries to learn,
per installed game, its install directory and (on Linux) its Proton prefix.
"""
from __future__ import annotations

import dataclasses
import re
from pathlib import Path

from . import steam

# appmanifest_*.acf is Valve KeyValues; we only need a few top-level fields.
_ACF_APPID = re.compile(r'"appid"\s*"(\d+)"')
_ACF_INSTALLDIR = re.compile(r'"installdir"\s*"([^"]+)"')
_ACF_NAME = re.compile(r'"name"\s*"([^"]+)"')


@dataclasses.dataclass
class InstalledGame:
    appid: str
    name: str
    install_name: str       # the folder name under steamapps/common
    install_dir: Path       # full path to the install directory
    prefix: Path | None     # compatdata/<appid>/pfx, if it exists (Proton)
    library: Path

    @property
    def root(self) -> str:
        """The ``<root>`` value: the Steam library folder (holds steamapps,
        and userdata for the main library)."""
        return str(self.library).replace("\\", "/")

    @property
    def base(self) -> str:
        """The ``<base>`` value: the game's full install directory."""
        return str(self.install_dir).replace("\\", "/")


class SteamIndex:
    """All Steam games installed on this machine, keyed by app id (string)."""

    def __init__(self, by_appid: dict[str, InstalledGame]):
        self.by_appid = by_appid

    def get(self, appid: int | str | None) -> InstalledGame | None:
        if appid is None:
            return None
        return self.by_appid.get(str(appid))

    def __len__(self) -> int:
        return len(self.by_appid)

    @classmethod
    def build(cls) -> "SteamIndex":
        by_appid: dict[str, InstalledGame] = {}
        for lib in steam.library_dirs():
            steamapps = lib / "steamapps"
            if not steamapps.is_dir():
                continue
            for acf in steamapps.glob("appmanifest_*.acf"):
                game = _parse_acf(acf, lib)
                if game is not None:
                    by_appid.setdefault(game.appid, game)
        return cls(by_appid)


def _parse_acf(acf: Path, library: Path) -> InstalledGame | None:
    try:
        text = acf.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    m_app = _ACF_APPID.search(text)
    m_dir = _ACF_INSTALLDIR.search(text)
    if not m_app or not m_dir:
        return None
    appid = m_app.group(1)
    install_name = m_dir.group(1)
    name = (_ACF_NAME.search(text) or m_dir).group(1)
    install_dir = library / "steamapps" / "common" / install_name
    pfx = library / "steamapps" / "compatdata" / appid / "pfx"
    return InstalledGame(
        appid=appid,
        name=name,
        install_name=install_name,
        install_dir=install_dir,
        prefix=pfx if pfx.exists() else None,
        library=library,
    )
