"""Tkinter GUI for the luduclone client.

Two tabs sharing one server connection:
  * Back up  -- scan this machine and upload found saves
  * Restore  -- probe the server, list available saves, and download+install
                them individually or all at once

Operations run on a worker thread so the window stays responsive; output streams
to a log box and a progress bar. Tkinter is stdlib, so this bundles into the exe.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .api import ApiClient
from .backup import detect_env, run_backup
from .config import ClientConfig, CONFIG_PATH
from .restore import restore_game
from .roots import SteamIndex
from .version import __version__
from . import updater
from shared import manifest as manifest_mod


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        updater.cleanup_old()  # clear any leftover *.old from a prior self-update
        root.title(f"luduclone {__version__}")
        root.geometry("720x600")
        root.minsize(620, 480)

        self._log_q: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._prog: tuple[int, int, str] | None = None
        self._remote_games: list[dict] = []
        self._buttons: list[ttk.Button] = []

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
        self.mode_var = tk.StringVar(value="auto")
        self.noreg_var = tk.BooleanVar(value=False)

        self._build(root)
        self.root.after(100, self._tick)
        # Non-blocking check for a newer release on startup.
        threading.Thread(target=self._check_updates, args=(False,), daemon=True).start()

    # ---- layout --------------------------------------------------------
    def _build(self, root: tk.Tk) -> None:
        pad = {"padx": 8, "pady": 4}

        menubar = tk.Menu(root)
        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="Check for updates…", command=self.on_check_updates)
        helpm.add_command(label="About", command=self.on_about)
        menubar.add_cascade(label="Help", menu=helpm)
        root.config(menu=menubar)

        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Server").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.server_var).grid(
            row=0, column=1, columnspan=3, sticky="we", padx=4)
        ttk.Label(top, text="Token").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.token_var, show="•").grid(
            row=1, column=1, columnspan=2, sticky="we", padx=4)
        ttk.Button(top, text="Save & connect", command=self.on_save).grid(
            row=1, column=3, sticky="e")
        top.columnconfigure(1, weight=1)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, **pad)
        nb.add(self._backup_tab(nb), text="Back up")
        nb.add(self._restore_tab(nb), text="Restore")

        prog = ttk.Frame(root)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.prog_label = ttk.Label(prog, text="", width=26, anchor="w")
        self.prog_label.pack(side="left", padx=8)

        self.log = tk.Text(root, height=9, wrap="word", state="disabled")
        self.log.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        self.status = ttk.Label(root, text="Ready", anchor="w", relief="sunken")
        self.status.pack(fill="x", side="bottom")

    def _backup_tab(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb)
        opts = ttk.Frame(f)
        opts.pack(fill="x", pady=6)
        ttk.Label(opts, text="Only game (optional)").pack(side="left")
        ttk.Entry(opts, textvariable=self.game_var, width=22).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Include config", variable=self.config_var).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Refresh manifest", variable=self.refresh_var).pack(side="left", padx=6)
        btns = ttk.Frame(f)
        btns.pack(fill="x")
        self._mkbtn(btns, "Scan (preview)", self.on_scan).pack(side="left")
        self._mkbtn(btns, "Back up & upload", self.on_backup).pack(side="left", padx=6)
        return f

    def _restore_tab(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        self._mkbtn(bar, "Refresh from server", self.on_refresh_remote).pack(side="left")
        self._mkbtn(bar, "Restore selected", self.on_restore_selected).pack(side="left", padx=6)
        self._mkbtn(bar, "Restore all", self.on_restore_all).pack(side="left")
        ttk.Label(bar, text="Mode").pack(side="left", padx=(12, 2))
        ttk.Combobox(bar, textvariable=self.mode_var, width=8, state="readonly",
                     values=("auto", "proton", "native", "windows")).pack(side="left")
        ttk.Checkbutton(bar, text="No registry", variable=self.noreg_var).pack(side="left", padx=6)

        cols = ("versions", "latest", "updated")
        self.tree = ttk.Treeview(f, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text="Game")
        self.tree.heading("versions", text="Versions")
        self.tree.heading("latest", text="Latest")
        self.tree.heading("updated", text="Last updated")
        self.tree.column("#0", width=300)
        self.tree.column("versions", width=70, anchor="center")
        self.tree.column("latest", width=60, anchor="center")
        self.tree.column("updated", width=160)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.on_restore_selected())
        return f

    def _mkbtn(self, parent, text, cmd) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=cmd)
        self._buttons.append(b)
        return b

    # ---- log + progress (thread-safe) ----------------------------------
    def log_line(self, text: str) -> None:
        self._log_q.put(text)

    def set_progress(self, current: int, total: int, label: str) -> None:
        self._prog = (current, total, label)

    def reset_progress(self) -> None:
        self._prog = None

    def set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def _tick(self) -> None:
        try:
            while True:
                line = self._log_q.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", line + "\n")
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        p = self._prog
        if p is None:
            self.progress["value"] = 0
            self.prog_label.configure(text="")
        else:
            cur, tot, label = p
            if tot > 0:
                pct = max(0, min(100, 100 * cur / tot))
                self.progress["value"] = pct
                self.prog_label.configure(text=f"{label}  {pct:.0f}%")
            else:
                self.prog_label.configure(text=label)
        self.root.after(100, self._tick)

    # ---- worker plumbing ----------------------------------------------
    def _run(self, fn, *args) -> None:
        if self._busy:
            return
        self._busy = True
        self._toggle(False)
        self.set_status("Working…")

        def worker():
            try:
                fn(*args)
            except Exception as e:  # noqa: BLE001
                self.log_line(f"ERROR: {e}")
            finally:
                self._busy = False
                self.reset_progress()
                self.root.after(0, lambda: self._toggle(True))
                self.root.after(0, lambda: self.set_status("Ready"))

        threading.Thread(target=worker, daemon=True).start()

    def _toggle(self, enabled: bool) -> None:
        for b in self._buttons:
            b.configure(state="normal" if enabled else "disabled")

    def _cfg(self) -> ClientConfig | None:
        server = self.server_var.get().strip()
        if not server:
            messagebox.showerror("luduclone", "Enter a server URL first.")
            return None
        return ClientConfig(server=server.rstrip("/"),
                            token=self.token_var.get().strip() or None)

    def _load_manifest(self, cfg: ClientConfig):
        api = ApiClient(cfg)
        if self.refresh_var.get() or not cfg.manifest_cache.exists():
            self.log_line("Downloading manifest from server…")
            api.fetch_manifest(progress=lambda d, t: self.set_progress(d, t, "Downloading manifest"))
        manifest = manifest_mod.Manifest.from_yaml(cfg.manifest_cache.read_text(encoding="utf-8"))
        return api, manifest

    def _tags(self):
        return {"save", "config"} if self.config_var.get() else {"save"}

    def _only(self):
        g = self.game_var.get().strip()
        return [g] if g else None

    # ---- config / connect ---------------------------------------------
    def on_save(self) -> None:
        cfg = self._cfg()
        if not cfg:
            return
        cfg.save()
        self.set_status(f"Saved to {CONFIG_PATH}")
        self._run(self._connect, cfg)

    def _connect(self, cfg: ClientConfig) -> None:
        self.log_line(f"Connected: {ApiClient(cfg).health()}")
        self._refresh_remote(cfg)

    # ---- backup --------------------------------------------------------
    def on_scan(self) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._scan, cfg)

    def _scan(self, cfg: ClientConfig) -> None:
        api, manifest = self._load_manifest(cfg)
        env = detect_env()
        self.log_line(f"Scanning {len(manifest)} games on {env.os}…")
        report = run_backup(api, manifest, env, tags=self._tags(), only=self._only(),
                            dry_run=True,
                            progress=lambda i, t, n: self.set_progress(i, t, "Scanning"))
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
        report = run_backup(api, manifest, env, tags=self._tags(), only=self._only(),
                            progress=lambda i, t, n: self.set_progress(i, t, "Backing up"))
        uploaded = [r for r in report if r["status"] == "uploaded"]
        for r in sorted(uploaded, key=lambda r: r["game"]):
            reg = f"  +{r['registry']} reg" if r.get("registry") else ""
            self.log_line(f"  uploaded {r['game']}  v{r['version']}  {r['files']} files, "
                          f"{_human(r['bytes'])}{reg}")
        self.log_line(f"Done. Uploaded {len(uploaded)} game(s).")
        self._refresh_remote(cfg)

    # ---- restore -------------------------------------------------------
    def on_refresh_remote(self) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._refresh_remote, cfg)

    def _refresh_remote(self, cfg: ClientConfig) -> None:
        games = ApiClient(cfg).list_games()
        self._remote_games = games
        self.root.after(0, self._populate_tree)
        self.log_line(f"Server has {len(games)} game(s) with backups.")

    def _populate_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for g in sorted(self._remote_games, key=lambda g: g["game"].lower()):
            self.tree.insert("", "end", iid=g["game"], text=g["game"],
                             values=(g["versions"], f"v{g['latest']}", _fmt_time(g.get("updated"))))

    def on_restore_selected(self) -> None:
        names = list(self.tree.selection())
        if not names:
            messagebox.showinfo("luduclone", "Select one or more games in the list first.")
            return
        self._start_restore(names)

    def on_restore_all(self) -> None:
        names = [g["game"] for g in self._remote_games]
        if not names:
            messagebox.showinfo("luduclone", "Refresh from the server first.")
            return
        if not messagebox.askyesno("luduclone", f"Restore all {len(names)} game(s)?"):
            return
        self._start_restore(names)

    def _start_restore(self, names: list[str]) -> None:
        cfg = self._cfg()
        if cfg:
            self._run(self._restore, cfg, names)

    def _restore(self, cfg: ClientConfig, names: list[str]) -> None:
        api, manifest = self._load_manifest(cfg)
        index = SteamIndex.build()
        total = len(names)
        self.log_line(f"Restoring {total} game(s) (mode: {self.mode_var.get()})…")
        ok = 0
        for i, name in enumerate(names, 1):
            self.set_progress(i, total, "Restoring")
            res = restore_game(api, manifest, name, mode=self.mode_var.get(),
                               do_registry=not self.noreg_var.get(), steam_index=index)
            detail = f" ({res.detail})" if res.detail else ""
            self.log_line(f"  {name}: {res.status}"
                          + (f" via {res.mode}" if res.mode else "") + detail)
            for o in res.entries:
                if o.status != "restored":
                    self.log_line(f"      [{o.status}] {o.template}")
            if res.status == "restored":
                ok += 1
        self.log_line(f"Done. Restored {ok}/{total} game(s).")


    # ---- updates -------------------------------------------------------
    def on_check_updates(self) -> None:
        self._run(self._check_updates, True)

    def on_about(self) -> None:
        messagebox.showinfo(
            "About luduclone",
            f"luduclone {__version__}\n\nSelf-hosted cross-OS game-save sync.\n"
            f"https://github.com/{updater.GITHUB_REPO}")

    def _check_updates(self, manual: bool) -> None:
        try:
            rel = updater.update_available()
        except Exception as e:  # noqa: BLE001
            if manual:
                self._popup(lambda: messagebox.showerror("luduclone", f"Update check failed: {e}"))
            return
        if rel is None:
            if manual:
                self._popup(lambda: messagebox.showinfo(
                    "luduclone", f"You are up to date (v{updater.current_version()})."))
            return
        self.log_line(f"Update available: {rel.tag} (you have v{updater.current_version()}).")
        if not updater.is_frozen():
            if manual:
                self._popup(lambda: messagebox.showinfo(
                    "luduclone", f"Update {rel.tag} available. Running from source — "
                    "use 'git pull' to update."))
            return
        self._popup(lambda: self._offer_update(rel))

    def _offer_update(self, rel) -> None:
        if messagebox.askyesno(
                "luduclone update",
                f"Update {rel.tag} is available (you have v{updater.current_version()}).\n"
                "Download and install it now?"):
            self._run(self._do_update, rel)

    def _do_update(self, rel) -> None:
        self.log_line(f"Downloading {rel.tag}…")
        exe = updater.apply_update(
            rel, progress=lambda d, t: self.set_progress(d, t, "Updating"))
        self.log_line(f"Updated to {rel.tag}. Restart to use the new version.")
        self._popup(lambda: messagebox.showinfo(
            "luduclone", f"Updated to {rel.tag}.\nPlease restart {exe.name}."))

    def _popup(self, fn) -> None:
        """Schedule a dialog on the Tk main thread."""
        self.root.after(0, fn)


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}B"


def _fmt_time(epoch) -> str:
    if not epoch:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def main() -> int:
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
