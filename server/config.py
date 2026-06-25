"""Server configuration, read from environment variables."""
from __future__ import annotations

import os
from pathlib import Path


class Config:
    # Where save bundles, the sqlite db, and the manifest cache live.
    DATA_DIR = Path(os.environ.get("LUDUCLONE_DATA_DIR", "/data"))

    # "user1:token1,user2:token2" -> {token: user}. If empty, auth is OPEN
    # (every request is treated as user "default") -- convenient for first run,
    # but set tokens before exposing the server.
    _RAW_TOKENS = os.environ.get("LUDUCLONE_TOKENS", "").strip()

    # How often the server refreshes its manifest copy from upstream.
    MANIFEST_MAX_AGE = int(os.environ.get("LUDUCLONE_MANIFEST_MAX_AGE", str(24 * 3600)))

    @property
    def db_path(self) -> Path:
        return self.DATA_DIR / "luduclone.db"

    @property
    def saves_dir(self) -> Path:
        return self.DATA_DIR / "saves"

    @property
    def manifest_cache(self) -> Path:
        return self.DATA_DIR / "manifest.yaml"

    @property
    def tokens(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for pair in self._RAW_TOKENS.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            user, token = pair.split(":", 1)
            out[token.strip()] = user.strip()
        return out

    @property
    def auth_open(self) -> bool:
        return not self.tokens


config = Config()
