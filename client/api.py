"""Thin HTTP client for the luduclone server."""
from __future__ import annotations

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

    def fetch_manifest(self) -> str:
        """Download the manifest from the server and cache it locally."""
        r = self.session.get(self._url("/manifest"), timeout=120)
        r.raise_for_status()
        self.cfg.manifest_cache.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.manifest_cache.write_text(r.text, encoding="utf-8")
        return r.text

    def list_games(self) -> list[dict]:
        r = self.session.get(self._url("/games"), timeout=30)
        r.raise_for_status()
        return r.json()["games"]

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
        """Download a bundle to ``dest``; returns the metadata from headers."""
        suffix = f"/{version}" if version is not None else "/latest"
        with self.session.get(
            self._url(f"/games/{game}/saves{suffix}"), stream=True, timeout=600
        ) as r:
            r.raise_for_status()
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    f.write(chunk)
            meta = {
                "version": r.headers.get("X-Luduclone-Version"),
                "source_os": r.headers.get("X-Luduclone-Source-Os"),
                "sha256": r.headers.get("X-Luduclone-Sha256"),
                "mapping": json.loads(r.headers.get("X-Luduclone-Mapping", "{}")),
            }
        return meta
