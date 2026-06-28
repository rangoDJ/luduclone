"""Resolve ludusavi path placeholders into concrete (possibly globbed) paths.

Placeholders like ``<winAppData>`` are expanded against the *target* environment.
``<storeUserId>`` and ``<osUserName>`` are not knowable up front, so they are
emitted as ``*`` and resolved later by globbing the real filesystem.

Three resolution environments are supported:

* ``windows``    -- a real Windows machine (used by the Windows client to back up)
* ``linux``      -- native Linux paths (XDG dirs, $HOME)
* ``proton``     -- Windows placeholders re-rooted into a Steam Proton/Wine prefix
                    (used by the Linux client to restore Windows-only games).

The resolver returns a path with forward slashes and ``*`` for unknown segments;
callers feed that into :mod:`shared.scan` to enumerate matches.
"""
from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path

# Matches <placeholderName> tokens.
_TOKEN = re.compile(r"<([a-zA-Z]+)>")

# Placeholders whose value we cannot know in advance -> glob wildcard.
_WILDCARD = "*"


@dataclasses.dataclass
class Env:
    """Resolution context for one target environment.

    Attributes mirror the directories ludusavi needs. For ``proton`` targets,
    ``prefix`` points at ``compatdata/<id>/pfx`` and the win* dirs are computed
    relative to ``<prefix>/drive_c/users/steamuser``.
    """

    os: str                      # "windows" | "linux"
    home: str
    # Windows-style dirs (populated for windows + proton)
    win_app_data: str | None = None
    win_local_app_data: str | None = None
    win_local_app_data_low: str | None = None
    win_documents: str | None = None
    win_public: str | None = None
    win_program_data: str | None = None
    win_dir: str | None = None
    # Linux XDG dirs
    xdg_data: str | None = None
    xdg_config: str | None = None
    # Optional store ids
    steam_game_id: str | None = None
    # When known (e.g. "steamuser" inside a Proton prefix) this replaces the
    # <osUserName> wildcard so restore targets a concrete path.
    os_user_name: str | None = None

    @classmethod
    def detect_windows(cls) -> "Env":
        home = os.environ.get("USERPROFILE") or str(Path.home())
        return cls(
            os="windows",
            home=home,
            win_app_data=os.environ.get("APPDATA", f"{home}/AppData/Roaming"),
            win_local_app_data=os.environ.get("LOCALAPPDATA", f"{home}/AppData/Local"),
            win_local_app_data_low=f"{home}/AppData/LocalLow",
            win_documents=f"{home}/Documents",
            win_public=os.environ.get("PUBLIC", "C:/Users/Public"),
            win_program_data=os.environ.get("PROGRAMDATA", "C:/ProgramData"),
            win_dir=os.environ.get("WINDIR", "C:/Windows"),
        )

    @classmethod
    def detect_linux(cls) -> "Env":
        home = os.environ.get("HOME") or str(Path.home())
        return cls(
            os="linux",
            home=home,
            xdg_data=os.environ.get("XDG_DATA_HOME", f"{home}/.local/share"),
            xdg_config=os.environ.get("XDG_CONFIG_HOME", f"{home}/.config"),
        )

    @classmethod
    def for_proton_prefix(cls, prefix: str | os.PathLike) -> "Env":
        """Build a windows-style Env re-rooted inside a Proton/Wine prefix.

        ``prefix`` is the ``pfx`` directory (contains ``drive_c``). Inside Proton
        the game's "Windows user" is ``steamuser``.
        """
        prefix = str(prefix).replace("\\", "/").rstrip("/")
        user = f"{prefix}/drive_c/users/steamuser"
        return cls(
            os="windows",  # the *paths* are windows-shaped even though host is linux
            home=user,
            win_app_data=f"{user}/AppData/Roaming",
            win_local_app_data=f"{user}/AppData/Local",
            win_local_app_data_low=f"{user}/AppData/LocalLow",
            win_documents=f"{user}/Documents",
            win_public=f"{prefix}/drive_c/users/Public",
            win_program_data=f"{prefix}/drive_c/ProgramData",
            win_dir=f"{prefix}/drive_c/windows",
            os_user_name="steamuser",
        )


def resolve(template: str, env: Env, *, game_install_dir: str | None = None,
            root: str | None = None, base: str | None = None,
            store_user_id: str | None = None, store_game_id: str | None = None) -> str:
    """Expand a manifest path template into a concrete globbable path.

    ``base``/``root``/``game_install_dir`` are independent (matching ludusavi):
    ``<base>`` is the game's full install dir, ``<root>`` is the library/root
    path, ``<game>`` is the install-dir name. If ``base`` is omitted it falls
    back to ``<root>/<game>``. Unknown user-specific segments become ``*``.
    """
    table = _table(env, game_install_dir=game_install_dir, root=root, base=base,
                   store_user_id=store_user_id, store_game_id=store_game_id)

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name not in table:
            # Unknown placeholder: leave a wildcard so we don't silently mismatch.
            return _WILDCARD
        value = table[name]
        return value if value is not None else _WILDCARD

    # <base> expands to <root>/<game>, which may themselves contain placeholders,
    # so resolve iteratively until stable (bounded to avoid pathological loops).
    out = template.replace("\\", "/")
    for _ in range(5):
        new = _TOKEN.sub(sub, out)
        if new == out:
            break
        out = new
    # Collapse accidental duplicate slashes (but preserve a leading // is rare on win).
    out = re.sub(r"(?<!:)//+", "/", out)
    return out


def _table(env: Env, *, game_install_dir: str | None, root: str | None,
           base: str | None = None, store_user_id: str | None = None,
           store_game_id: str | None = None) -> dict[str, str | None]:
    game = game_install_dir or _WILDCARD
    root_val = root or _WILDCARD
    base_val = base or f"{root_val}/{game}"
    return {
        "home": env.home,
        "base": base_val,
        "root": root_val,
        "game": game,
        "osUserName": env.os_user_name or _WILDCARD,
        "storeUserId": store_user_id or _WILDCARD,
        "storeGameId": store_game_id or env.steam_game_id or _WILDCARD,
        "winAppData": env.win_app_data,
        "winLocalAppData": env.win_local_app_data,
        "winLocalAppDataLow": env.win_local_app_data_low,
        "winDocuments": env.win_documents,
        "winPublic": env.win_public,
        "winProgramData": env.win_program_data,
        "winDir": env.win_dir,
        "xdgData": env.xdg_data,
        "xdgConfig": env.xdg_config,
    }
