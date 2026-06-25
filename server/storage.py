"""Persistence: SQLite metadata + filesystem save bundles.

Layout under DATA_DIR:
    luduclone.db                          # metadata
    saves/<user>/<game>/<version>.tar.gz  # opaque save bundles produced by clients
"""
from __future__ import annotations

import dataclasses
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

_SAFE = re.compile(r"[^A-Za-z0-9._ -]")


def _slug(name: str) -> str:
    """Filesystem-safe directory name for a user/game (DB keeps the real name)."""
    s = _SAFE.sub("_", name).strip().strip(".")
    return s or "_"


@dataclasses.dataclass
class SaveRecord:
    id: int
    user: str
    game: str
    version: int
    created_at: float
    source_os: str
    sha256: str
    size: int
    mapping: str          # JSON string: how files map back to manifest placeholders
    path: str             # bundle path relative to DATA_DIR

    def to_public(self) -> dict:
        return {
            "version": self.version,
            "created_at": self.created_at,
            "source_os": self.source_os,
            "sha256": self.sha256,
            "size": self.size,
        }


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.saves_dir = self.data_dir / "saves"
        self.db_path = self.data_dir / "luduclone.db"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.saves_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS saves (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    user        TEXT NOT NULL,
                    game        TEXT NOT NULL,
                    version     INTEGER NOT NULL,
                    created_at  REAL NOT NULL,
                    source_os   TEXT NOT NULL,
                    sha256      TEXT NOT NULL,
                    size        INTEGER NOT NULL,
                    mapping     TEXT NOT NULL,
                    path        TEXT NOT NULL,
                    UNIQUE(user, game, version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_saves_user_game ON saves(user, game)"
            )

    # ----- writes -------------------------------------------------------
    def bundle_path(self, user: str, game: str, version: int) -> Path:
        return self.saves_dir / _slug(user) / _slug(game) / f"v{version}.tar.gz"

    def next_version(self, user: str, game: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(version) AS m FROM saves WHERE user=? AND game=?",
                (user, game),
            ).fetchone()
        return (row["m"] or 0) + 1

    def add_save(self, *, user: str, game: str, version: int, source_os: str,
                 sha256: str, size: int, mapping: str, path: Path) -> SaveRecord:
        created = time.time()
        rel = str(path.relative_to(self.data_dir).as_posix())
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO saves
                   (user, game, version, created_at, source_os, sha256, size, mapping, path)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (user, game, version, created, source_os, sha256, size, mapping, rel),
            )
            sid = cur.lastrowid
        return SaveRecord(sid, user, game, version, created, source_os,
                          sha256, size, mapping, rel)

    # ----- reads --------------------------------------------------------
    def list_games(self, user: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT game, COUNT(*) AS versions, MAX(version) AS latest,
                          MAX(created_at) AS updated
                   FROM saves WHERE user=? GROUP BY game ORDER BY game""",
                (user,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_versions(self, user: str, game: str) -> list[SaveRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM saves WHERE user=? AND game=? ORDER BY version DESC",
                (user, game),
            ).fetchall()
        return [self._row(r) for r in rows]

    def get_save(self, user: str, game: str,
                 version: Optional[int] = None) -> Optional[SaveRecord]:
        with self._connect() as conn:
            if version is None:
                row = conn.execute(
                    """SELECT * FROM saves WHERE user=? AND game=?
                       ORDER BY version DESC LIMIT 1""",
                    (user, game),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM saves WHERE user=? AND game=? AND version=?",
                    (user, game, version),
                ).fetchone()
        return self._row(row) if row else None

    def _row(self, r: sqlite3.Row) -> SaveRecord:
        return SaveRecord(
            id=r["id"], user=r["user"], game=r["game"], version=r["version"],
            created_at=r["created_at"], source_os=r["source_os"], sha256=r["sha256"],
            size=r["size"], mapping=r["mapping"], path=r["path"],
        )

    def abs_path(self, rec: SaveRecord) -> Path:
        return self.data_dir / rec.path

    # ----- deletes ------------------------------------------------------
    def delete_game(self, user: str, game: str) -> int:
        """Delete all versions of a game for a user, including bundle files.
        Returns the number of versions removed."""
        import shutil

        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM saves WHERE user=? AND game=?",
                (user, game),
            ).fetchone()
            count = row["n"] or 0
            if count:
                conn.execute("DELETE FROM saves WHERE user=? AND game=?", (user, game))
        bundle_dir = self.saves_dir / _slug(user) / _slug(game)
        if bundle_dir.exists():
            shutil.rmtree(bundle_dir, ignore_errors=True)
        return count
