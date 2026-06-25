"""Tkinter GUI for the luduclone client (mainly the Windows backup side).

Wraps the same backup/scan/remote operations as the CLI, running them on a
worker thread so the window stays responsive, and streaming output to a log box.
Tkinter is in the Python stdlib, so this bundles into the exe with no extra deps.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .api import ApiClient
from .backup import detect_env, run_backup
from .config import ClientConfig, CONFIG_PATH
from shared import manifest as manifest_mod


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("luduclone")
        root.geometry("680x520")
        root.minsize(560, 420)

        self._log_q: queue.Queue[str] = queue.Queue()
        self._busy = False

        # Pre-fill from saved config / env if present.
        server, token = "", ""
        try:
            cfg = ClientConfig.load()
            server, token = cfg.server, cfg.token or ""
        except SystemExit:
            pass

        self.server_var = tk.StringVar(value=server)
        self.token_var = tk.StringVar(value=token)
        self.game_var = tk.StringVar()
        self.config_var = tk.BooleanVar(value=False)
        self.refresh_var = tk.BooleanVar(value=False)

        self._build(root)
        self.root.after(100, self._drain_log)

    # ---- layout --------------------------------------------------------
    def _build(self, root: tk.Tk) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Server").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.server_var, width=44).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)
        ttk.Label(top, text="Token").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.token_var, width=44, show="•").grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(top, text="Save config", command=self.on_save).grid(
            row=1, column=3, sticky="e")
        top.columnconfigure(1, weight=1)

        opts = ttk.Frame(root)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Only game (optional)").pack(side="left")
        ttk.Entry(opts, textvariable=self.game_var, width=24).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Include config files",
                        variable=self.config_var).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Refresh manifest",
                        variable=self.refresh_var).pack(side="left", padx=6)

        btns = ttk.Frame(root)
        btns.pack(fill="x", **pad)
        self.scan_btn = ttk.Button(btns, text="Scan (preview)", command=self.on_scan)
        self.scan_btn.pack(side="left")
        self.backup_btn = ttk.Button(btns, text="Back up & upload", command=self.on_backup)
        self.backup_btn.pack(side="left", padx=6)
        self.remote_btn = ttk.Button(btns, text="Server games", command=self.on_remote)
        self.remote_btn.pack(side="left")
        ttk.Button(btns, text="Clear log", command=self.clear_log).pack(side="right")

        self.log = tk.Text(root, height=18, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 4))

        self.status = ttk.Label(root, text="Ready", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    # ---- logging (thread-safe via queue) -------------------------------
    def log_line(self, text: str) -> None:
        self._log_q.put(text)

    def _drain_log(self) -> None:
        try:
            while True:
                line = self._log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._drain_log)

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    # ---- config --------------------------------------------------------
    def _cfg(self) -> ClientConfig | None:
        server = self.server_var.get().strip()
        if not server:
            messagebox.showerror("luduclone", "Enter a server URL first.")
            return None
        return ClientConfig(server=server.rstrip("/"),
                            token=self.token_var.get().strip() or None)

    def on_save(self) -> None:
        cfg = self._cfg()
        if not cfg:
            return
        cfg.save()
        self.set_status(f"Saved to {CONFIG_PATH}")
        # Verify connectivity in the background.
        self._run(self._verify, cfg)

    # ---- operations (run on worker thread) -----------------------------
    def _run(self, fn, *args) -> None:
        if self._busy:
            return
        self._busy = True
        self._toggle_buttons(False)
        self.set_status("Working…")

        def worker():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"ERROR: {e}")
            finally:
                self._busy = False
                self.root.after(0, lambda: self._toggle_buttons(True))
                self.root.after(0, lambda: self.set_status("Ready"))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle_buttons(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for b in (self.scan_btn, self.backup_btn, self.remote_btn):
            b.configure(state=state)

    def _load_manifest(self, cfg: ClientConfig):
        api = ApiClient(cfg)
        if self.refresh_var.get() or not cfg.manifest_cache.exists():
            self.log_line("Fetching manifest from server…")
            api.fetch_manifest()
        manifest = manifest_mod.Manifest.from_yaml(
            cfg.manifest_cache.read_text(encoding="utf-8"))
        return api, manifest

    def _tags(self):
        return {"save", "config"} if self.config_var.get() else {"save"}

    def _only(self):
        g = self.game_var.get().strip()
        return [g] if g else None

    def _verify(self, cfg: ClientConfig) -> None:
        health = ApiClient(cfg).health()
        self.log_line(f"Connected: {health}")

    def on_scan(self) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._scan, cfg)

    def _scan(self, cfg: ClientConfig) -> None:
        api, manifest = self._load_manifest(cfg)
        env = detect_env()
        self.log_line(f"Scanning {len(manifest)} games on {env.os}… (this can take a moment)")
        report = run_backup(api, manifest, env, tags=self._tags(),
                            only=self._only(), dry_run=True)
        if not report:
            self.log_line("No save data found on this machine.")
            return
        self.log_line(f"Found saves for {len(report)} game(s):")
        for r in sorted(report, key=lambda r: r["game"]):
            reg = f"  +{r['registry']} reg" if r.get("registry") else ""
            self.log_line(f"  {r['game']}  —  {r['files']} files, {_human(r['bytes'])}{reg}")

    def on_backup(self) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._backup, cfg)

    def _backup(self, cfg: ClientConfig) -> None:
        api, manifest = self._load_manifest(cfg)
        env = detect_env()
        self.log_line(f"Backing up from {env.os}…")
        report = run_backup(api, manifest, env, tags=self._tags(), only=self._only())
        uploaded = [r for r in report if r["status"] == "uploaded"]
        for r in sorted(uploaded, key=lambda r: r["game"]):
            reg = f"  +{r['registry']} reg" if r.get("registry") else ""
            self.log_line(f"  uploaded {r['game']}  v{r['version']}  "
                          f"{r['files']} files, {_human(r['bytes'])}{reg}")
        self.log_line(f"Done. Uploaded {len(uploaded)} game(s).")

    def on_remote(self) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._remote, cfg)

    def _remote(self, cfg: ClientConfig) -> None:
        games = ApiClient(cfg).list_games()
        if not games:
            self.log_line("No backups on the server yet.")
            return
        self.log_line(f"{len(games)} game(s) on the server:")
        for g in games:
            self.log_line(f"  {g['game']}  —  {g['versions']} version(s), latest v{g['latest']}")


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}B"


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
