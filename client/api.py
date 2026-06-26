"""Thin HTTP client for the luduclone server."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

import requests

from .config import ClientConfig


class ApiClient:
    def __init__(self, cfg: ClientConfig):
        self.cfg = cfg
        self.session = requests.Session()
        if cfg.token:
            self.session.headers["Authorization"] = f"Bearer {cfg.token}"

    def _url(self, path: str) -> str:
        return f"{self.cfg.server}{path}"

    def health(self) -> dict:
        r = self.session.get(self._url("/health"), timeout=30)
        r.raise_for_status()
        return r.json()

    def fetch_manifest(self, progress=None) -> str:
        """Download the manifest from the server and cache it locally.

        ``progress`` is an optional callback ``(downloaded_bytes, total_bytes)``
        invoked as the body streams in; ``total_bytes`` is 0 if unknown.
        """
        with self.session.get(self._url("/manifest"), stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            chunks: list[bytes] = []
            for chunk in r.iter_content(chunk_size=16 * 1024):
                chunks.append(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
            text = b"".join(chunks).decode("utf-8")
        self.cfg.manifest_cache.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.manifest_cache.write_text(text, encoding="utf-8")
        return text

    def list_games(self) -> list[dict]:
        r = self.session.get(self._url("/games"), timeout=30)
        r.raise_for_status()
        return r.json()["games"]

    def delete_game(self, game: str) -> dict:
        r = self.session.delete(self._url(f"/games/{game}"), timeout=30)
        r.raise_for_status()
        return r.json()

    def upload(self, game: str, bundle_path: Path, source_os: str, mapping: dict) -> dict:
        with open(bundle_path, "rb") as f:
            r = self.session.post(
                self._url(f"/games/{game}/saves"),
                data={"source_os": source_os, "mapping": json.dumps(mapping)},
                files={"bundle": (f"{game}.tar.gz", f, "application/gzip")},
                timeout=600,
            )
        r.raise_for_status()
        return r.json()

    def download_latest(self, game: str, dest: Path,
                        version: Optional[int] = None) -> dict:
        """Download a bundle to ``dest``; returns the metadata from headers.

        Verifies the payload against the server-reported SHA-256 so a truncated
        or corrupted download is never handed to the restore step.
        """
        suffix = f"/{version}" if version is not None else "/latest"
        sha = hashlib.sha256()
        with self.session.get(
            self._url(f"/games/{game}/saves{suffix}"), stream=True, timeout=600
        ) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    sha.update(chunk)
                    f.write(chunk)
            meta = {
                "version": r.headers.get("X-Luduclone-Version"),
                "source_os": r.headers.get("X-Luduclone-Source-Os"),
                "sha256": r.headers.get("X-Luduclone-Sha256"),
                "mapping": json.loads(r.headers.get("X-Luduclone-Mapping", "{}")),
            }
        expected = meta.get("sha256")
        if expected and sha.hexdigest() != expected:
            dest.unlink(missing_ok=True)
            raise IOError(f"Checksum mismatch for {game} "
                          f"(expected {expected[:12]}…, got {sha.hexdigest()[:12]}…)")
        return meta
