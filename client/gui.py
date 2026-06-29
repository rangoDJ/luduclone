"""Tkinter GUI for the luduclone client.

A ludusavi-style window: each tab shows one searchable, checkable game list with
an expandable file tree (per-entry, with sizes) and a running summary of what is
selected. Tabs share one server connection:

  * Back up  -- scan this machine, tick the games to upload, back them up
  * Restore  -- probe the server, tick the games to download, restore them

Operations run on a worker thread so the window stays responsive; output streams
to a log box and a progress bar. Tkinter is stdlib, so this bundles into the exe.

The look is modernised with the Sun Valley (Windows 11 Fluent) ttk theme plus
Segoe UI and per-process DPI awareness; a View menu toggles light/dark (defaulting
to the OS setting). All of that degrades gracefully if ``sv_ttk``/``darkdetect``
aren't installed.
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import tkinter.font as tkfont

# Optional, for a modern Windows 11 (Fluent) look. Both are pure-Python and
# bundled into the packaged exe; when absent (source install without them) the
# GUI still runs, just with the default Tk theme.
try:
    import sv_ttk  # Sun Valley ttk theme (Win11 light/dark)
except Exception:  # noqa: BLE001
    sv_ttk = None
try:
    import darkdetect  # follow the OS light/dark setting
except Exception:  # noqa: BLE001
    darkdetect = None

from .api import ApiClient
from .backup import detect_env, run_backup
from .config import ClientConfig, CONFIG_PATH
from .custom import CustomConfig
from .restore import restore_game
from .roots import SteamIndex
from .version import __version__
from . import updater
from shared import manifest as manifest_mod

CHECKED = "☑"      # ☑
UNCHECKED = "☐"    # ☐


class GameList(ttk.Frame):
    """A searchable, checkable, expandable list of games.

    Top-level rows are games with a checkbox in the first column; each game can
    be expanded to reveal its detail rows (save entries and individual files).
    Column headers sort the games; the checkbox header toggles all. A filter box
    hides games whose name doesn't match the typed text.
    """

    def __init__(self, parent, columns, *, on_change=None):
        """``columns`` is a list of (key, heading, width, anchor, numeric)."""
        super().__init__(parent)
        self._cols = columns
        self._on_change = on_change
        self.checked: set[str] = set()
        self.meta: dict[str, dict] = {}     # game name -> arbitrary metadata
        self._sort_state: dict[str, bool] = {}

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(0, 4))
        ttk.Label(bar, text="Filter").pack(side="left")
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<KeyRelease>", lambda e: self._apply_filter())
        ttk.Button(bar, text="All", width=4,
                   command=lambda: self._set_all(True)).pack(side="left")
        ttk.Button(bar, text="None", width=5,
                   command=lambda: self._set_all(False)).pack(side="left", padx=(2, 0))

        # The leading tree column (#0) is the checkbox; the game name and the
        # caller's columns follow. This puts the tick at the far left (ludusavi).
        keys = [c[0] for c in columns]
        self.tree = ttk.Treeview(self, columns=("name", *keys),
                                 show="tree headings", selectmode="none")
        self.tree.heading("#0", text="",
                          command=lambda: self._set_all(self._mostly_unchecked()))
        self.tree.column("#0", width=54, minwidth=54, anchor="w", stretch=False)
        self.tree.heading("name", text="Game",
                          command=lambda: self._sort_by("name", False))
        self.tree.column("name", width=300, anchor="w")
        for key, heading, width, anchor, numeric in columns:
            self.tree.heading(key, text=heading,
                              command=lambda k=key, n=numeric: self._sort_by(k, n))
            self.tree.column(key, width=width, anchor=anchor, stretch=False)

        vsb = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)

    # ---- population ----------------------------------------------------
    def clear(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.checked.clear()
        self.meta.clear()
        self._fire()

    def add_game(self, name: str, values: tuple, *, checked: bool = True,
                 meta: dict | None = None, children: list | None = None) -> None:
        """Add one game row. ``children`` is a (possibly nested) list of
        ``{"text": str, "values": tuple, "children": [...]}`` detail rows."""
        glyph = CHECKED if checked else UNCHECKED
        self.tree.insert("", "end", iid=name, text=glyph, values=(name, *values))
        if checked:
            self.checked.add(name)
        self.meta[name] = meta or {}
        for child in children or []:
            self._add_child(name, child)

    def set_children(self, name: str, children: list) -> None:
        """Replace a game's detail rows (e.g. with a fresh restore diff)."""
        if name not in self.meta:
            return
        for c in self.tree.get_children(name):
            self.tree.delete(c)
        for child in children or []:
            self._add_child(name, child)
        self.tree.item(name, open=True)

    def _add_child(self, parent_iid: str, node: dict) -> None:
        # Children have no checkbox (#0 empty); their label goes in the name
        # column so it lines up under the game, indented by the tree.
        pad = ("",) * len(self._cols)
        vals = node.get("values") or pad
        iid = self.tree.insert(parent_iid, "end", text="",
                               values=(node.get("text", ""), *vals))
        for sub in node.get("children") or []:
            self._add_child(iid, sub)

    def finish(self) -> None:
        self._apply_filter()
        self._fire()

    # ---- checkbox handling --------------------------------------------
    def _on_click(self, event) -> None:
        # The checkbox lives in the tree column (#0); a click there toggles the
        # game, except on the expand/collapse triangle (the "indicator" element).
        if self.tree.identify_column(event.x) != "#0":
            return
        iid = self.tree.identify_row(event.y)
        if not iid or self.tree.parent(iid):             # only top-level games
            return
        if "indicator" in str(self.tree.identify_element(event.x, event.y)):
            return
        self._toggle(iid)

    def _toggle(self, iid: str) -> None:
        if iid in self.checked:
            self.checked.discard(iid)
            glyph = UNCHECKED
        else:
            self.checked.add(iid)
            glyph = CHECKED
        self.tree.item(iid, text=glyph)
        self._fire()

    def _set_all(self, on: bool) -> None:
        for iid in self.tree.get_children():
            want = on and (iid in self._visible())
            is_on = iid in self.checked
            if want != is_on:
                self._toggle(iid)

    def _mostly_unchecked(self) -> bool:
        vis = self._visible()
        return len(self.checked & vis) * 2 <= len(vis)

    def _visible(self) -> set[str]:
        return set(self.tree.get_children())

    def get_checked(self) -> list[str]:
        # All checked games, including any currently hidden by the filter, so a
        # search term never silently drops a selection.
        return [g for g in sorted(self.meta, key=str.lower) if g in self.checked]

    # ---- filtering -----------------------------------------------------
    def _apply_filter(self) -> None:
        needle = self.search_var.get().strip().lower()
        # Reattach matches in their original (alphabetical) order, detach the rest.
        for name in sorted(self.meta, key=str.lower):
            if not needle or needle in name.lower():
                self.tree.reattach(name, "", "end")
            else:
                self.tree.detach(name)
        self._fire()

    # ---- sorting -------------------------------------------------------
    def _sort_by(self, col: str, numeric: bool) -> None:
        desc = self._sort_state.get(col, False)
        items = list(self.tree.get_children())

        def keyfn(iid):
            raw = self.meta.get(iid, {}).get(f"sort_{col}")
            if raw is None:
                raw = self.tree.set(iid, col)
            return raw if numeric else str(raw).lower()

        items.sort(key=keyfn, reverse=desc)
        for i, iid in enumerate(items):
            self.tree.move(iid, "", i)
        self._sort_state[col] = not desc

    # ---- summary -------------------------------------------------------
    def _fire(self) -> None:
        if self._on_change:
            self._on_change()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        updater.cleanup_old()  # clear any leftover *.old from a prior self-update
        root.title(f"luduclone {__version__}")
        # Scale the window to the display DPI so it isn't tiny on a HiDPI/scaled
        # monitor once per-process DPI awareness is on (see main()).
        scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        root.geometry(f"{int(800 * scale)}x{int(660 * scale)}")
        root.minsize(int(680 * scale), int(540 * scale))

        self._log_q: queue.Queue[str] = queue.Queue()
        self._busy = False
        self._prog: tuple[int, int, str] | None = None
        self._remote_games: list[dict] = []
        self._buttons: list[ttk.Button] = []
        self._classic_widgets: list[tk.Widget] = []   # tk.Text/Listbox to recolor
        self.theme_var = tk.StringVar(
            value=(darkdetect.theme().lower() if darkdetect and darkdetect.theme()
                   else "light"))

        server, token, retain = "", "", 0
        try:
            cfg = ClientConfig.load()
            server, token, retain = cfg.server, cfg.token or "", cfg.retain
        except SystemExit:
            pass

        self.server_var = tk.StringVar(value=server)
        self.token_var = tk.StringVar(value=token)
        self.retain_var = tk.StringVar(value=str(retain))
        self.config_var = tk.BooleanVar(value=False)
        self.refresh_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="auto")
        self.noreg_var = tk.BooleanVar(value=False)
        self.custom = CustomConfig.load()

        self._build(root)
        self.root.after(100, self._tick)
        threading.Thread(target=self._check_updates, args=(False,), daemon=True).start()

    # ---- layout --------------------------------------------------------
    def _build(self, root: tk.Tk) -> None:
        pad = {"padx": 8, "pady": 4}

        self._init_fonts()

        menubar = tk.Menu(root)
        viewm = tk.Menu(menubar, tearoff=0)
        viewm.add_radiobutton(label="Light", value="light", variable=self.theme_var,
                              command=lambda: self._set_theme("light"))
        viewm.add_radiobutton(label="Dark", value="dark", variable=self.theme_var,
                              command=lambda: self._set_theme("dark"))
        menubar.add_cascade(label="View", menu=viewm)
        helpm = tk.Menu(menubar, tearoff=0)
        helpm.add_command(label="Check for updates…", command=self.on_check_updates)
        helpm.add_command(label="About", command=self.on_about)
        menubar.add_cascade(label="Help", menu=helpm)
        root.config(menu=menubar)
        self._menus = [menubar, viewm, helpm]

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
        nb.add(self._custom_tab(nb), text="Custom")

        ttk.Separator(root, orient="horizontal").pack(fill="x", padx=8, pady=(2, 0))

        prog = ttk.Frame(root)
        prog.pack(fill="x", padx=8, pady=4)
        ttk.Label(prog, text="Activity").pack(side="left")
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True, padx=8)
        self.prog_label = ttk.Label(prog, text="", width=26, anchor="e")
        self.prog_label.pack(side="left")

        self.log = tk.Text(root, height=5, wrap="word", state="disabled",
                           relief="flat", borderwidth=8, highlightthickness=0)
        self.log.pack(fill="x", expand=False, padx=8, pady=(0, 4))
        self._classic_widgets.append(self.log)

        self.status = ttk.Label(root, text="Ready", anchor="w", padding=(8, 4))
        self.status.pack(fill="x", side="bottom")

        self._set_theme(self.theme_var.get())   # applies sv-ttk + classic colors

    # ---- look & feel ---------------------------------------------------
    def _init_fonts(self) -> None:
        """Use Segoe UI (the Windows system font) across stock + ttk widgets."""
        family = "Segoe UI" if os.name == "nt" else "TkDefaultFont"
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                f = tkfont.nametofont(name)
                if os.name == "nt":
                    f.configure(family=family, size=10)
            except tk.TclError:
                pass

    def _set_theme(self, name: str) -> None:
        """Apply the Sun Valley (Win11) theme and recolor the stock tk widgets
        (Text/Listbox), which ttk themes don't reach, to match."""
        if sv_ttk is not None:
            try:
                sv_ttk.set_theme(name)
            except tk.TclError:
                pass
        dark = name == "dark"
        bg = "#1c1c1c" if dark else "#ffffff"
        fg = "#e6e6e6" if dark else "#1a1a1a"
        sel = "#2f5d8c" if dark else "#cfe3ff"
        for w in self._classic_widgets:
            try:
                w.configure(background=bg, foreground=fg, highlightthickness=0,
                            selectbackground=sel,
                            selectforeground=("#ffffff" if dark else "#000000"))
                if isinstance(w, tk.Text):
                    w.configure(insertbackground=fg)
            except tk.TclError:
                pass
        # Stock tk menus aren't reached by the ttk theme; colour them to match.
        menu_bg = "#2b2b2b" if dark else "#f3f3f3"
        for m in getattr(self, "_menus", []):
            try:
                m.configure(background=menu_bg, foreground=fg,
                            activebackground=sel, activeforeground=fg,
                            borderwidth=0)
            except tk.TclError:
                pass
        self._apply_titlebar(dark)

    def _apply_titlebar(self, dark: bool) -> None:
        """Match the Windows title bar to the theme (Win10 2004+/Win11)."""
        if os.name != "nt":
            return
        try:
            from ctypes import windll, byref, c_int, sizeof
            self.root.update_idletasks()
            hwnd = windll.user32.GetParent(self.root.winfo_id())
            val = c_int(1 if dark else 0)
            for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (new, then old)
                windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, byref(val), sizeof(val))
            # Nudge a repaint of the caption, but only once the window is on
            # screen (during initial build it isn't mapped yet).
            if self.root.winfo_ismapped():
                self.root.withdraw()
                self.root.deiconify()
        except Exception:  # noqa: BLE001
            pass

    def _backup_tab(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb)
        opts = ttk.Frame(f)
        opts.pack(fill="x", pady=6)
        self._mkbtn(opts, "Scan this PC", self.on_scan).pack(side="left")
        self._mkbtn(opts, "Back up checked", self.on_backup).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Include config", variable=self.config_var).pack(side="left", padx=6)
        ttk.Checkbutton(opts, text="Refresh manifest", variable=self.refresh_var).pack(side="left", padx=6)
        ttk.Label(opts, text="Keep last").pack(side="left", padx=(6, 2))
        ttk.Spinbox(opts, from_=0, to=999, width=4,
                    textvariable=self.retain_var).pack(side="left")
        ttk.Label(opts, text="(0 = all)").pack(side="left", padx=(2, 0))
        self.backup_list = GameList(
            f,
            [("files", "Files", 70, "center", True),
             ("size", "Size", 90, "e", True)],
            on_change=self._update_backup_summary,
        )
        self.backup_list.pack(fill="both", expand=True)
        self.backup_summary = ttk.Label(f, text="Scan to discover saves on this machine.")
        self.backup_summary.pack(fill="x", pady=(4, 0))
        return f

    def _restore_tab(self, nb) -> ttk.Frame:
        f = ttk.Frame(nb)
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=6)
        self._mkbtn(bar, "Refresh from server", self.on_refresh_remote).pack(side="left")
        self._mkbtn(bar, "Preview", self.on_preview_restore).pack(side="left", padx=6)
        self._mkbtn(bar, "Restore checked", self.on_restore_selected).pack(side="left")
        ttk.Label(bar, text="Mode").pack(side="left", padx=(12, 2))
        ttk.Combobox(bar, textvariable=self.mode_var, width=8, state="readonly",
                     values=("auto", "proton", "native", "windows")).pack(side="left")
        ttk.Checkbutton(bar, text="No registry", variable=self.noreg_var).pack(side="left", padx=6)
        self.restore_list = GameList(
            f,
            [("versions", "Versions", 70, "center", True),
             ("latest", "Latest", 60, "center", True),
             ("updated", "Last updated", 150, "w", False)],
            on_change=self._update_restore_summary,
        )
        self.restore_list.pack(fill="both", expand=True)
        self.restore_summary = ttk.Label(f, text="Refresh from the server to list backups.")
        self.restore_summary.pack(fill="x", pady=(4, 0))
        return f

    def _custom_tab(self, nb) -> ttk.Frame:
        """Editors for custom games, restore redirects, and backup ignores —
        ludusavi's 'Custom games' / 'Other' settings. Saved immediately; a fresh
        Scan / Refresh picks up the changes."""
        f = ttk.Frame(nb)

        gframe = ttk.LabelFrame(f, text="Custom games (extra/overriding manifest)")
        gframe.pack(fill="both", expand=True, padx=4, pady=4)
        self.cg_list = tk.Listbox(gframe, height=6, relief="flat", borderwidth=6,
                                  activestyle="none")
        self.cg_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        gb = ttk.Frame(gframe)
        gb.pack(side="left", fill="y", padx=4)
        ttk.Button(gb, text="Add…", command=self.on_add_custom_game).pack(fill="x")
        ttk.Button(gb, text="Remove", command=self.on_rm_custom_game).pack(fill="x", pady=2)

        rframe = ttk.LabelFrame(f, text="Restore redirects (source path → target path)")
        rframe.pack(fill="both", expand=True, padx=4, pady=4)
        self.rd_list = tk.Listbox(rframe, height=4, relief="flat", borderwidth=6,
                                  activestyle="none")
        self.rd_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        rb = ttk.Frame(rframe)
        rb.pack(side="left", fill="y", padx=4)
        ttk.Button(rb, text="Add…", command=self.on_add_redirect).pack(fill="x")
        ttk.Button(rb, text="Remove", command=self.on_rm_redirect).pack(fill="x", pady=2)

        iframe = ttk.LabelFrame(f, text="Backup ignore globs (e.g. */cache/*, *.tmp)")
        iframe.pack(fill="both", expand=True, padx=4, pady=4)
        self.ig_list = tk.Listbox(iframe, height=4, relief="flat", borderwidth=6,
                                  activestyle="none")
        self.ig_list.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        ib = ttk.Frame(iframe)
        ib.pack(side="left", fill="y", padx=4)
        ttk.Button(ib, text="Add…", command=self.on_add_ignore).pack(fill="x")
        ttk.Button(ib, text="Remove", command=self.on_rm_ignore).pack(fill="x", pady=2)

        self._classic_widgets += [self.cg_list, self.rd_list, self.ig_list]
        self._refresh_custom_lists()
        return f

    def _refresh_custom_lists(self) -> None:
        self.cg_list.delete(0, "end")
        for g in self.custom.games:
            sid = f", steam {g['steam_id']}" if g.get("steam_id") else ""
            self.cg_list.insert("end", f"{g.get('name')}  "
                                f"({len(g.get('files') or [])} path(s){sid})")
        self.rd_list.delete(0, "end")
        for r in self.custom.redirects:
            self.rd_list.insert("end", f"{r.get('source')}  →  {r.get('target')}")
        self.ig_list.delete(0, "end")
        for pat in self.custom.ignores:
            self.ig_list.insert("end", pat)

    def on_add_custom_game(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title("Add custom game")
        dlg.transient(self.root)
        dlg.grab_set()
        name_var = tk.StringVar()
        sid_var = tk.StringVar()
        ttk.Label(dlg, text="Name").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Entry(dlg, textvariable=name_var, width=46).grid(row=0, column=1, padx=6, pady=4)
        ttk.Label(dlg, text="Save paths\n(one per line)").grid(row=1, column=0, sticky="nw", padx=6)
        paths_txt = tk.Text(dlg, width=50, height=6)
        paths_txt.grid(row=1, column=1, padx=6, pady=4)
        ttk.Label(dlg, text="Registry keys\n(one per line)").grid(row=2, column=0, sticky="nw", padx=6)
        reg_txt = tk.Text(dlg, width=50, height=3)
        reg_txt.grid(row=2, column=1, padx=6, pady=4)
        ttk.Label(dlg, text="Steam app id\n(optional)").grid(row=3, column=0, sticky="w", padx=6)
        ttk.Entry(dlg, textvariable=sid_var, width=14).grid(row=3, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(dlg, text="Paths accept manifest placeholders like <home>, <winDocuments>.",
                  foreground="gray").grid(row=4, column=0, columnspan=2, padx=6)

        def save():
            name = name_var.get().strip()
            if not name:
                messagebox.showerror("luduclone", "Enter a name.", parent=dlg)
                return
            files = [ln.strip() for ln in paths_txt.get("1.0", "end").splitlines() if ln.strip()]
            regs = [ln.strip() for ln in reg_txt.get("1.0", "end").splitlines() if ln.strip()]
            sid = sid_var.get().strip()
            self.custom.games = [g for g in self.custom.games if g.get("name") != name]
            self.custom.games.append({"name": name, "files": files, "registry": regs,
                                      "steam_id": int(sid) if sid.isdigit() else None})
            self.custom.save()
            self._refresh_custom_lists()
            dlg.destroy()

        btns = ttk.Frame(dlg)
        btns.grid(row=5, column=0, columnspan=2, pady=8)
        ttk.Button(btns, text="Save", command=save).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

    def on_rm_custom_game(self) -> None:
        i = self._sel(self.cg_list)
        if i is not None:
            del self.custom.games[i]
            self.custom.save()
            self._refresh_custom_lists()

    def on_add_redirect(self) -> None:
        src = simpledialog.askstring("Redirect", "Source path prefix:", parent=self.root)
        if not src:
            return
        tgt = simpledialog.askstring("Redirect", "Target path prefix:", parent=self.root)
        if not tgt:
            return
        self.custom.redirects.append({"source": src.strip(), "target": tgt.strip()})
        self.custom.save()
        self._refresh_custom_lists()

    def on_rm_redirect(self) -> None:
        i = self._sel(self.rd_list)
        if i is not None:
            del self.custom.redirects[i]
            self.custom.save()
            self._refresh_custom_lists()

    def on_add_ignore(self) -> None:
        pat = simpledialog.askstring("Ignore", "Glob pattern (e.g. */cache/*):",
                                     parent=self.root)
        if pat and pat.strip():
            self.custom.ignores.append(pat.strip())
            self.custom.save()
            self._refresh_custom_lists()

    def on_rm_ignore(self) -> None:
        i = self._sel(self.ig_list)
        if i is not None:
            del self.custom.ignores[i]
            self.custom.save()
            self._refresh_custom_lists()

    @staticmethod
    def _sel(listbox: tk.Listbox):
        sel = listbox.curselection()
        return sel[0] if sel else None

    def _mkbtn(self, parent, text, cmd) -> ttk.Button:
        b = ttk.Button(parent, text=text, command=cmd)
        self._buttons.append(b)
        return b

    # ---- summaries -----------------------------------------------------
    def _update_backup_summary(self) -> None:
        gl = self.backup_list
        checked = gl.get_checked()
        files = sum(gl.meta[g].get("files", 0) for g in checked)
        size = sum(gl.meta[g].get("bytes", 0) for g in checked)
        total = len(gl.meta)
        self.backup_summary.configure(
            text=f"{len(checked)} of {total} game(s) checked  —  "
                 f"{files} files, {_human(size)}")

    def _update_restore_summary(self) -> None:
        gl = self.restore_list
        checked = gl.get_checked()
        total = len(gl.meta)
        self.restore_summary.configure(
            text=f"{len(checked)} of {total} game(s) checked for restore")

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
        retain = self.retain_var.get().strip()
        return ClientConfig(server=server.rstrip("/"),
                            token=self.token_var.get().strip() or None,
                            retain=int(retain) if retain.isdigit() else 0)

    def _load_manifest(self, cfg: ClientConfig):
        api = ApiClient(cfg)
        if self.refresh_var.get() or not cfg.manifest_cache.exists():
            self.log_line("Downloading manifest from server…")
            api.fetch_manifest(progress=lambda d, t: self.set_progress(d, t, "Downloading manifest"))
        manifest = manifest_mod.Manifest.from_yaml(cfg.manifest_cache.read_text(encoding="utf-8"))
        CustomConfig.load().merge_into(manifest)
        return api, manifest

    def _tags(self):
        return {"save", "config"} if self.config_var.get() else {"save"}

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
        report = run_backup(api, manifest, env, tags=self._tags(), dry_run=True,
                            progress=lambda i, t, n: self.set_progress(i, t, "Scanning"))
        self.root.after(0, lambda: self._populate_backup(report))
        self.log_line(f"Found saves for {len(report)} game(s).")

    def _populate_backup(self, report: list[dict]) -> None:
        gl = self.backup_list
        gl.clear()
        for r in sorted(report, key=lambda r: r["game"].lower()):
            children = [_entry_node(e) for e in r.get("entries", [])]
            if r.get("registry"):
                children.append({"text": f"+{r['registry']} registry key(s)",
                                 "values": ("", "")})
            gl.add_game(
                r["game"], (r["files"], _human(r["bytes"])),
                meta={"files": r["files"], "bytes": r["bytes"],
                      "sort_files": r["files"], "sort_size": r["bytes"]},
                children=children,
            )
        gl.finish()

    def on_backup(self) -> None:
        cfg = self._cfg()
        if not cfg:
            return
        names = self.backup_list.get_checked()
        if not names:
            messagebox.showinfo("luduclone", "Scan, then check the games to back up.")
            return
        self._run(self._backup, cfg, names)

    def _backup(self, cfg: ClientConfig, names: list[str]) -> None:
        api, manifest = self._load_manifest(cfg)
        env = detect_env()
        self.log_line(f"Backing up {len(names)} game(s) from {env.os}…")
        report = run_backup(api, manifest, env, tags=self._tags(), only=names,
                            progress=lambda i, t, n: self.set_progress(i, t, "Backing up"))
        uploaded = [r for r in report if r["status"] == "uploaded"]
        for r in sorted(uploaded, key=lambda r: r["game"]):
            reg = f"  +{r['registry']} reg" if r.get("registry") else ""
            pruned = f"  (pruned {len(r['pruned'])} old)" if r.get("pruned") else ""
            self.log_line(f"  uploaded {r['game']}  v{r['version']}  {r['files']} files, "
                          f"{_human(r['bytes'])}{reg}{pruned}")
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
        self.root.after(0, self._populate_restore)
        self.log_line(f"Server has {len(games)} game(s) with backups.")

    def _populate_restore(self) -> None:
        gl = self.restore_list
        gl.clear()
        for g in sorted(self._remote_games, key=lambda g: g["game"].lower()):
            updated = _fmt_time(g.get("updated"))
            gl.add_game(
                g["game"], (g["versions"], f"v{g['latest']}", updated),
                meta={"sort_versions": g["versions"], "sort_latest": g["latest"],
                      "sort_updated": g.get("updated") or 0},
            )
        gl.finish()

    def on_preview_restore(self) -> None:
        names = self.restore_list.get_checked()
        if not names:
            messagebox.showinfo("luduclone", "Refresh, then check the games to preview.")
            return
        cfg = self._cfg()
        if cfg:
            self._run(self._preview_restore, cfg, names)

    def _preview_restore(self, cfg: ClientConfig, names: list[str]) -> None:
        api, manifest = self._load_manifest(cfg)
        index = SteamIndex.build()
        total = len(names)
        self.log_line(f"Previewing restore of {total} game(s) (mode: {self.mode_var.get()})…")
        for i, name in enumerate(names, 1):
            self.set_progress(i, total, "Previewing")
            res = restore_game(api, manifest, name, mode=self.mode_var.get(),
                               preview=True, do_registry=not self.noreg_var.get(),
                               steam_index=index)
            self.root.after(0, lambda n=name, r=res: self.restore_list.set_children(
                n, _diff_nodes(r)))
            new = sum(o.new for o in res.entries)
            changed = sum(o.changed for o in res.entries)
            self.log_line(f"  {name}: {res.status}"
                          + (f" via {res.mode}" if res.mode else "")
                          + f"  ({new} new, {changed} changed)")
        self.log_line("Preview done. Expand a game to see the file diff.")

    def on_restore_selected(self) -> None:
        names = self.restore_list.get_checked()
        if not names:
            messagebox.showinfo("luduclone", "Refresh, then check the games to restore.")
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
            new = sum(o.new for o in res.entries)
            changed = sum(o.changed for o in res.entries)
            same = sum(o.identical for o in res.entries)
            if res.status == "restored":
                self.log_line(f"      {new} new, {changed} changed, {same} unchanged")
            if res.undo_dir:
                self.log_line(f"      undo: {res.undo_dir}")
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


def _diff_nodes(res) -> list:
    """Tree rows describing a restore preview: a header, then one row per entry
    with its changed/new files nested beneath."""
    head = f"mode: {res.mode or '-'}   target: {res.target_root or '-'}"
    nodes = [{"text": head}]
    for o in res.entries:
        if o.status != "restored":
            nodes.append({"text": f"[{o.status}] {o.template}"})
            continue
        files = [{"text": f"{d['status']}: {d['rel']}  ({_human(d['size'])})"}
                 for d in o.file_diffs if d["status"] != "identical"]
        label = f"{o.template}  —  {o.new} new, {o.changed} changed, {o.identical} same"
        nodes.append({"text": label, "children": files})
    if res.registry_files:
        nodes.append({"text": f"registry: {', '.join(res.registry_files)}"})
    return nodes


def _entry_node(entry: dict) -> dict:
    """Build a tree node for one save entry, with its files nested beneath."""
    files = entry.get("files", [])
    total = sum(f.get("size", 0) for f in files)
    file_nodes = [{"text": f["path"], "values": ("", _human(f.get("size", 0)))}
                  for f in files]
    return {"text": entry["template"], "values": (len(files), _human(total)),
            "children": file_nodes}


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


def _enable_hidpi() -> None:
    """Tell Windows this process is DPI-aware so Tk renders crisp (not bitmap-
    stretched/blurry) text on scaled displays. No-op off Windows / on failure."""
    if os.name != "nt":
        return
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)   # system DPI aware
    except Exception:  # noqa: BLE001
        try:
            from ctypes import windll
            windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    _enable_hidpi()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
