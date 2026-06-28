"""Client configuration: server URL + auth token.

Resolution order (first wins): explicit CLI flag -> environment variable ->
config file at ~/.config/luduclone/config.json (or %APPDATA% on Windows).
"""
from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path


def _config_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData/Roaming")
        return Path(base) / "luduclone"
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "luduclone"


CONFIG_PATH = _config_dir() / "config.json"


@dataclasses.dataclass
class ClientConfig:
    server: str
    token: str | None = None
    # Requested version cap sent on upload (0 = let the server decide / unlimited).
    retain: int = 0
    # Local manifest cache so we don't refetch from the server every run.
    manifest_cache: Path = dataclasses.field(
        default_factory=lambda: _config_dir() / "manifest.yaml"
    )

    @classmethod
    def load(cls, server: str | None = None, token: str | None = None) -> "ClientConfig":
        file_cfg: dict = {}
        if CONFIG_PATH.exists():
            file_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        resolved_server = (
            server or os.environ.get("LUDUCLONE_SERVER") or file_cfg.get("server")
        )
        if not resolved_server:
            raise SystemExit(
                "No server configured. Pass --server, set LUDUCLONE_SERVER, "
                f"or write {CONFIG_PATH}."
            )
        resolved_token = (
            token or os.environ.get("LUDUCLONE_TOKEN") or file_cfg.get("token")
        )
        return cls(server=resolved_server.rstrip("/"), token=resolved_token,
                   retain=int(file_cfg.get("retain") or 0))

    def save(self) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps({"server": self.server, "token": self.token,
                        "retain": self.retain}, indent=2),
            encoding="utf-8",
        )
