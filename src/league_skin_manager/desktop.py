"""Native desktop presentation for browsing installed skins."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, cast

from .catalog import CatalogError, CatalogSnapshot, SkinRecord, load_catalog
from .controller import AppState

Action = Callable[[], object]
MigrationAction = Callable[[Path], object]
DirectorySelector = Callable[[Path], Path | None]
MigrationConfirmation = Callable[[Path], bool]
HistoryResetConfirmation = Callable[[], bool]
StartupGetter = Callable[[], bool]
StartupSetter = Callable[[bool], object]
PathOpener = Callable[[Path], object]
CatalogLoader = Callable[[Path], CatalogSnapshot]


def format_package_size(value: int) -> str:
    if value < 0:
        raise ValueError("package size cannot be negative")
    units = ("B", "KB", "MB", "GB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    raise AssertionError("unreachable")


class DesktopApplication:
    """Tk/ttk window whose public methods are safe from worker threads."""

    ALL_CHAMPIONS = "All champions"
    POLL_MILLISECONDS = 60
    FILTER_DEBOUNCE_MILLISECONDS = 120

    def __init__(
        self,
        *,
        catalog_path: Path,
        installed_dir: Path,
        data_dir: Path,
        log_file: Path,
        on_sync: Action,
        on_start_manager: Action,
        on_start_ltk: Action,
        on_migrate_to_ltk: MigrationAction,
        on_cancel_ltk_migration: Action,
        on_reset_ltk_migration: Action,
        on_exit: Action,
        startup_enabled: StartupGetter,
        set_startup_enabled: StartupSetter,
        path_opener: PathOpener,
        catalog_loader: CatalogLoader = load_catalog,
        directory_selector: DirectorySelector | None = None,
        migration_confirmation: MigrationConfirmation | None = None,
        history_reset_confirmation: HistoryResetConfirmation | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._catalog_path = catalog_path
        self._installed_dir = installed_dir
        self._data_dir = data_dir
        self._log_file = log_file
        self._on_sync = on_sync
        self._on_start_manager = on_start_manager
        self._on_start_ltk = on_start_ltk
        self._on_migrate_to_ltk = on_migrate_to_ltk
        self._on_cancel_ltk_migration = on_cancel_ltk_migration
        self._on_reset_ltk_migration = on_reset_ltk_migration
        self._on_exit = on_exit
        self._startup_enabled = startup_enabled
        self._set_startup_enabled = set_startup_enabled
        self._path_opener = path_opener
        self._catalog_loader = catalog_loader
        self._directory_selector = directory_selector
        self._migration_confirmation = migration_confirmation
        self._history_reset_confirmation = history_reset_confirmation
        self._logger = logger or logging.getLogger(__name__)

        self._events: Queue[tuple[str, object | None]] = Queue()
        self._catalog = CatalogSnapshot(source_commit="", patch=None, skins=())
        self._root: Any | None = None
        self._tree: Any | None = None
        self._search_var: Any | None = None
        self._champion_var: Any | None = None
        self._result_var: Any | None = None
        self._status_var: Any | None = None
        self._stats_var: Any | None = None
        self._detail_var: Any | None = None
        self._startup_var: Any | None = None
        self._ltk_status_var: Any | None = None
        self._cancel_migration_button: Any | None = None
        self._champion_box: Any | None = None
        self._filter_after_id: str | None = None
        self._rows: dict[str, SkinRecord] = {}
        self._sort_column = "champion"
        self._sort_descending = False
        self._exit_pending = False
        self._exit_thread: Thread | None = None

    def run(self, *, show_on_start: bool = True) -> None:
        """Create the native window and enter its main loop on this thread."""

        import tkinter as tk
        from tkinter import ttk

        root = tk.Tk()
        self._root = root
        self._build_window(root, tk, ttk)
        self._load_catalog_now()
        if show_on_start:
            root.deiconify()
            root.lift()
        else:
            root.withdraw()
        root.after(self.POLL_MILLISECONDS, self._drain_events)
        root.mainloop()

    def show(self) -> None:
        self._events.put(("show", None))

    def hide(self) -> None:
        self._events.put(("hide", None))

    def stop(self) -> None:
        self._events.put(("stop", None))

    def refresh_catalog(self) -> None:
        self._events.put(("refresh", None))

    def update_status(self, state: AppState, detail: str) -> None:
        self._events.put(("status", (state, detail)))

    def request_ltk_migration(self) -> None:
        """Show and run the migration chooser on Tk's owning thread."""

        self._events.put(("migrate_request", None))

    def update_ltk_status(self, detail: str, *, migration_active: bool = False) -> None:
        """Publish companion progress without replacing skin-sync state."""

        self._events.put(("ltk_status", (detail, migration_active)))

    def _build_window(self, root: Any, tk: Any, ttk: Any) -> None:
        root.title("LeagueSkinManagerVN")
        root.geometry("1120x720")
        root.minsize(880, 560)
        root.configure(background="#0b1220")
        root.protocol("WM_DELETE_WINDOW", self._hide_now)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#0b1220")
        style.configure("Panel.TFrame", background="#111c2e")
        style.configure(
            "Title.TLabel",
            background="#0b1220",
            foreground="#f8fafc",
            font=("Segoe UI Semibold", 22),
        )
        style.configure(
            "Subtitle.TLabel",
            background="#0b1220",
            foreground="#94a3b8",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Panel.TLabel",
            background="#111c2e",
            foreground="#dbeafe",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Status.TLabel",
            background="#111c2e",
            foreground="#60a5fa",
            font=("Segoe UI Semibold", 10),
        )
        style.configure(
            "Accent.TButton",
            background="#2563eb",
            foreground="#ffffff",
            borderwidth=0,
            padding=(14, 9),
            font=("Segoe UI Semibold", 10),
        )
        style.map("Accent.TButton", background=[("active", "#3b82f6")])
        style.configure(
            "Secondary.TButton",
            background="#1e293b",
            foreground="#e2e8f0",
            borderwidth=0,
            padding=(12, 8),
            font=("Segoe UI", 9),
        )
        style.map("Secondary.TButton", background=[("active", "#334155")])
        style.configure(
            "Treeview",
            background="#111c2e",
            fieldbackground="#111c2e",
            foreground="#e2e8f0",
            rowheight=29,
            borderwidth=0,
            font=("Segoe UI", 9),
        )
        style.configure(
            "Treeview.Heading",
            background="#1e293b",
            foreground="#cbd5e1",
            relief="flat",
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "Treeview",
            background=[("selected", "#1d4ed8")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure(
            "Dark.TEntry",
            fieldbackground="#0f172a",
            foreground="#f8fafc",
            insertcolor="#f8fafc",
            bordercolor="#334155",
            padding=9,
        )
        style.configure(
            "Dark.TCombobox",
            fieldbackground="#0f172a",
            foreground="#f8fafc",
            arrowcolor="#94a3b8",
            bordercolor="#334155",
            padding=7,
        )
        style.configure(
            "Dark.TCheckbutton",
            background="#111c2e",
            foreground="#cbd5e1",
            font=("Segoe UI", 9),
        )

        outer = ttk.Frame(root, style="App.TFrame", padding=24)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        title_group = ttk.Frame(header, style="App.TFrame")
        title_group.pack(side="left", fill="x", expand=True)
        ttk.Label(title_group, text="Skin Library", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_group,
            text="Browse verified skins and manage synchronization from one place.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        actions = ttk.Frame(header, style="App.TFrame")
        actions.pack(side="right")
        ttk.Button(
            actions,
            text="Sync now",
            style="Secondary.TButton",
            command=self._sync_clicked,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text="Start CSLOL Manager",
            style="Accent.TButton",
            command=self._manager_clicked,
        ).pack(side="left", padx=(0, 8))
        ttk.Button(
            actions,
            text="Open / install LTK",
            style="Secondary.TButton",
            command=self._ltk_clicked,
        ).pack(side="left")

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        panel.pack(fill="both", expand=True)

        self._status_var = tk.StringVar(value="Starting")
        self._stats_var = tk.StringVar(value="Loading installed catalog…")
        status_row = ttk.Frame(panel, style="Panel.TFrame")
        status_row.pack(fill="x", pady=(0, 14))
        ttk.Label(status_row, textvariable=self._status_var, style="Status.TLabel").pack(
            side="left"
        )
        ttk.Label(status_row, textvariable=self._stats_var, style="Panel.TLabel").pack(side="right")

        filters = ttk.Frame(panel, style="Panel.TFrame")
        filters.pack(fill="x", pady=(0, 12))
        self._search_var = tk.StringVar()
        search = ttk.Entry(
            filters,
            textvariable=self._search_var,
            style="Dark.TEntry",
            font=("Segoe UI", 10),
        )
        search.pack(side="left", fill="x", expand=True, padx=(0, 10))
        search.insert(0, "")
        self._search_var.trace_add("write", self._filter_changed)
        self._champion_var = tk.StringVar(value=self.ALL_CHAMPIONS)
        self._champion_box = ttk.Combobox(
            filters,
            textvariable=self._champion_var,
            state="readonly",
            width=25,
            style="Dark.TCombobox",
        )
        self._champion_box.pack(side="left")
        self._champion_box.bind("<<ComboboxSelected>>", self._filter_changed)

        table_frame = ttk.Frame(panel, style="Panel.TFrame")
        table_frame.pack(fill="both", expand=True)
        columns = ("champion", "skin", "size")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        self._tree = tree
        tree.heading("champion", text="Champion", command=lambda: self._sort_by("champion"))
        tree.heading("skin", text="Skin", command=lambda: self._sort_by("skin"))
        tree.heading("size", text="Package size", command=lambda: self._sort_by("size"))
        tree.column("champion", width=190, minwidth=130, anchor="w")
        tree.column("skin", width=560, minwidth=260, anchor="w")
        tree.column("size", width=130, minwidth=100, anchor="e")
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        tree.bind("<<TreeviewSelect>>", self._selection_changed)

        footer = ttk.Frame(panel, style="Panel.TFrame")
        footer.pack(fill="x", pady=(13, 0))
        info = ttk.Frame(footer, style="Panel.TFrame")
        info.pack(side="left", fill="x", expand=True)
        self._result_var = tk.StringVar(value="0 results")
        self._detail_var = tk.StringVar(value="Select a skin to view its installed folder.")
        ttk.Label(info, textvariable=self._result_var, style="Status.TLabel").pack(anchor="w")
        ttk.Label(info, textvariable=self._detail_var, style="Panel.TLabel").pack(
            anchor="w", pady=(3, 0)
        )
        footer_actions = ttk.Frame(footer, style="Panel.TFrame")
        footer_actions.pack(side="right")
        ttk.Button(
            footer_actions,
            text="Open selected folder",
            style="Secondary.TButton",
            command=self._open_selected,
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="App data",
            style="Secondary.TButton",
            command=lambda: self._open_path(self._data_dir),
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="Logs",
            style="Secondary.TButton",
            command=lambda: self._open_path(self._log_file),
        ).pack(side="left", padx=(0, 7))
        ttk.Button(
            footer_actions,
            text="Refresh",
            style="Secondary.TButton",
            command=self._load_catalog_now,
        ).pack(side="left")

        settings = ttk.Frame(outer, style="App.TFrame")
        settings.pack(fill="x", pady=(12, 0))
        try:
            startup_value = bool(self._startup_enabled())
        except Exception:
            self._logger.exception("Unable to read Start with Windows setting")
            startup_value = False
        self._startup_var = tk.BooleanVar(value=startup_value)
        ttk.Checkbutton(
            settings,
            text="Start with Windows in the background",
            variable=self._startup_var,
            command=self._startup_clicked,
            style="Dark.TCheckbutton",
        ).pack(side="left")
        ttk.Button(
            settings,
            text="Exit application",
            style="Secondary.TButton",
            command=self._exit_clicked,
        ).pack(side="right")

        companion = ttk.Frame(outer, style="App.TFrame")
        companion.pack(fill="x", pady=(8, 0))
        self._ltk_status_var = tk.StringVar(value="LTK companion: checking in background")
        ttk.Label(
            companion,
            textvariable=self._ltk_status_var,
            style="Subtitle.TLabel",
        ).pack(side="left", fill="x", expand=True)
        self._cancel_migration_button = ttk.Button(
            companion,
            text="Cancel migration",
            style="Secondary.TButton",
            command=self._cancel_migration_clicked,
            state="disabled",
        )
        self._cancel_migration_button.pack(side="right")
        ttk.Button(
            companion,
            text="Migrate CSLOL skins to LTK...",
            style="Secondary.TButton",
            command=self._migration_clicked,
        ).pack(side="right", padx=(0, 8))
        ttk.Button(
            companion,
            text="Reset migration history...",
            style="Secondary.TButton",
            command=self._reset_migration_history_clicked,
        ).pack(side="right", padx=(0, 8))

    def _drain_events(self) -> None:
        root = self._root
        if root is None:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "show":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                elif kind == "hide":
                    root.withdraw()
                elif kind == "stop":
                    root.destroy()
                    return
                elif kind == "refresh":
                    self._load_catalog_now()
                elif kind == "status":
                    state, detail = cast(tuple[AppState, str], payload)
                    if self._status_var is not None:
                        self._status_var.set(detail)
                    if state in (AppState.READY, AppState.OFFLINE_READY):
                        self._load_catalog_now()
                elif kind == "migrate_request":
                    root.deiconify()
                    root.lift()
                    root.focus_force()
                    self._migration_clicked()
                elif kind == "ltk_status":
                    detail, migration_active = cast(tuple[str, bool], payload)
                    if self._ltk_status_var is not None:
                        self._ltk_status_var.set(f"LTK companion: {detail}")
                    if self._cancel_migration_button is not None:
                        state_value = "normal" if migration_active else "disabled"
                        self._cancel_migration_button.configure(state=state_value)
                elif kind == "exit_complete":
                    self._exit_pending = False
                    self._exit_thread = None
                    if payload is True:
                        root.destroy()
                        return
                    if self._status_var is not None:
                        self._status_var.set("Background work is still stopping; try Exit again")
                elif kind == "exit_error":
                    self._exit_pending = False
                    self._exit_thread = None
                    if self._status_var is not None:
                        self._status_var.set(f"Could not exit: {payload}")
        except Empty:
            pass
        try:
            root.after(self.POLL_MILLISECONDS, self._drain_events)
        except Exception:
            return

    def _load_catalog_now(self) -> None:
        try:
            catalog = self._catalog_loader(self._catalog_path)
        except CatalogError as exc:
            self._logger.warning("Could not load desktop catalog: %s", exc)
            if self._status_var is not None:
                self._status_var.set(str(exc))
            return
        self._catalog = catalog
        if self._champion_box is not None:
            values = (self.ALL_CHAMPIONS, *catalog.champions)
            self._champion_box.configure(values=values)
            current = self._champion_var.get() if self._champion_var is not None else ""
            if current not in values and self._champion_var is not None:
                self._champion_var.set(self.ALL_CHAMPIONS)
        patch = catalog.patch or "unknown patch"
        commit = catalog.source_commit[:8] if catalog.source_commit else "not synced"
        if self._stats_var is not None:
            self._stats_var.set(
                f"{len(catalog.skins):,} skins  •  {len(catalog.champions):,} champions  •  "
                f"Patch {patch}  •  {commit}"
            )
        self._apply_filter()

    def _filter_changed(self, *_args: object) -> None:
        root = self._root
        if root is None:
            return
        if self._filter_after_id is not None:
            root.after_cancel(self._filter_after_id)
        self._filter_after_id = root.after(
            self.FILTER_DEBOUNCE_MILLISECONDS,
            self._apply_filter,
        )

    def _apply_filter(self) -> None:
        self._filter_after_id = None
        tree = self._tree
        if tree is None:
            return
        query = self._search_var.get() if self._search_var is not None else ""
        selected_champion = (
            self._champion_var.get() if self._champion_var is not None else self.ALL_CHAMPIONS
        )
        champion = None if selected_champion == self.ALL_CHAMPIONS else selected_champion
        records = list(self._catalog.filtered(query, champion))
        sorters: dict[str, Callable[[SkinRecord], Any]] = {
            "champion": lambda skin: (skin.champion.casefold(), skin.name.casefold()),
            "skin": lambda skin: (skin.name.casefold(), skin.champion.casefold()),
            "size": lambda skin: skin.size,
        }
        records.sort(key=sorters[self._sort_column], reverse=self._sort_descending)

        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._rows.clear()
        for index, skin in enumerate(records):
            item_id = str(index)
            self._rows[item_id] = skin
            tree.insert(
                "",
                "end",
                iid=item_id,
                values=(skin.champion, skin.name, format_package_size(skin.size)),
            )
        if self._result_var is not None:
            self._result_var.set(f"{len(records):,} result{'s' if len(records) != 1 else ''}")
        if self._detail_var is not None:
            self._detail_var.set("Select a skin to view its installed folder.")

    def _sort_by(self, column: str) -> None:
        if column == self._sort_column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        self._apply_filter()

    def _selection_changed(self, _event: object | None = None) -> None:
        skin = self._selected_skin()
        if skin is None or self._detail_var is None:
            return
        path = self._installed_dir / skin.directory
        self._detail_var.set(
            f"{skin.champion} • {skin.name} • {format_package_size(skin.size)} • {path}"
        )

    def _selected_skin(self) -> SkinRecord | None:
        tree = self._tree
        if tree is None:
            return None
        selection = tree.selection()
        if not selection:
            return None
        return self._rows.get(str(selection[0]))

    def _open_selected(self) -> None:
        skin = self._selected_skin()
        if skin is None:
            if self._status_var is not None:
                self._status_var.set("Select a skin first")
            return
        self._open_path(self._installed_dir / skin.directory)

    def _open_path(self, path: Path) -> None:
        try:
            self._path_opener(path)
        except Exception as exc:
            self._logger.exception("Could not open %s", path)
            if self._status_var is not None:
                self._status_var.set(f"Could not open {path.name}: {exc}")

    def _sync_clicked(self) -> None:
        try:
            if self._on_sync() is False and self._status_var is not None:
                self._status_var.set("Sync was not started")
        except Exception as exc:
            self._logger.exception("Desktop sync action failed")
            if self._status_var is not None:
                self._status_var.set(f"Could not start sync: {exc}")

    def _manager_clicked(self) -> None:
        try:
            if self._on_start_manager() is False and self._status_var is not None:
                self._status_var.set("CSLOL Manager could not be started")
        except Exception as exc:
            self._logger.exception("Desktop manager action failed")
            if self._status_var is not None:
                self._status_var.set(f"Could not start manager: {exc}")

    def _ltk_clicked(self) -> None:
        try:
            if self._on_start_ltk() is False and self._ltk_status_var is not None:
                self._ltk_status_var.set("LTK companion: launch was not started")
        except Exception as exc:
            self._logger.exception("Desktop LTK Manager action failed")
            if self._ltk_status_var is not None:
                self._ltk_status_var.set(f"LTK companion: could not start LTK Manager: {exc}")

    def _migration_clicked(self) -> None:
        try:
            source = self._choose_migration_source()
            if source is None:
                return
            if not self._confirm_migration(source):
                return
            if self._on_migrate_to_ltk(source) is False:
                if self._ltk_status_var is not None:
                    self._ltk_status_var.set("LTK companion: migration was not queued")
                return
            if self._ltk_status_var is not None:
                self._ltk_status_var.set("LTK companion: migration queued")
            if self._cancel_migration_button is not None:
                self._cancel_migration_button.configure(state="normal")
        except Exception as exc:
            self._logger.exception("Could not start CSLOL-to-LTK migration")
            if self._ltk_status_var is not None:
                self._ltk_status_var.set(f"LTK companion: could not start migration: {exc}")

    def _choose_migration_source(self) -> Path | None:
        if self._directory_selector is not None:
            return self._directory_selector(self._installed_dir)
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self._root,
            title="Choose CSLOL Manager folder or its installed folder",
            initialdir=str(self._installed_dir),
            mustexist=True,
        )
        return Path(selected) if selected else None

    def _confirm_migration(self, source: Path) -> bool:
        if self._migration_confirmation is not None:
            return self._migration_confirmation(source)
        from tkinter import messagebox

        root = self._root
        if root is None:
            return False

        return bool(
            messagebox.askyesno(
                "Migrate CSLOL skins to LTK",
                (
                    f"Source: {source}\n\n"
                    "LeagueSkinManagerVN will validate and queue these mods in LTK's archive "
                    "inbox, then open LTK Manager. CSLOL originals are left unchanged.\n\n"
                    "Close both CSLOL Manager and LTK Manager before continuing. Existing "
                    "and previously queued content is detected by SHA-256 and skipped. Continue?"
                ),
                parent=root,
            )
        )

    def _cancel_migration_clicked(self) -> None:
        try:
            if self._on_cancel_ltk_migration() is False:
                return
            if self._ltk_status_var is not None:
                self._ltk_status_var.set("LTK companion: cancelling migration safely...")
        except Exception as exc:
            self._logger.exception("Could not cancel LTK migration")
            if self._ltk_status_var is not None:
                self._ltk_status_var.set(f"LTK companion: cancellation failed: {exc}")

    def _reset_migration_history_clicked(self) -> None:
        try:
            if not self._confirm_history_reset():
                return
            if self._on_reset_ltk_migration() is False:
                if self._ltk_status_var is not None:
                    self._ltk_status_var.set("LTK companion: history reset was not queued")
                return
            if self._ltk_status_var is not None:
                self._ltk_status_var.set("LTK companion: migration-history reset queued")
        except Exception as exc:
            self._logger.exception("Could not reset LTK migration history")
            if self._ltk_status_var is not None:
                self._ltk_status_var.set(f"LTK companion: could not reset history: {exc}")

    def _confirm_history_reset(self) -> bool:
        if self._history_reset_confirmation is not None:
            return self._history_reset_confirmation()
        from tkinter import messagebox

        root = self._root
        if root is None:
            return False
        return bool(
            messagebox.askyesno(
                "Reset LTK migration history",
                (
                    "Reset LeagueSkinManagerVN's record of packages previously queued for LTK?\n\n"
                    "The next migration may queue skins that are already installed in LTK. "
                    "No CSLOL or LTK files will be deleted."
                ),
                parent=root,
            )
        )

    def _startup_clicked(self) -> None:
        if self._startup_var is None:
            return
        desired = bool(self._startup_var.get())
        try:
            if self._set_startup_enabled(desired) is False:
                raise RuntimeError("the setting was rejected")
        except Exception as exc:
            self._logger.exception("Unable to update Start with Windows setting")
            self._startup_var.set(not desired)
            if self._status_var is not None:
                self._status_var.set(f"Could not update startup setting: {exc}")

    def _exit_clicked(self) -> None:
        if self._exit_pending:
            return
        self._exit_pending = True
        if self._status_var is not None:
            self._status_var.set("Stopping application…")
        worker = Thread(
            target=self._run_exit_request,
            name="desktop-shutdown-request",
            daemon=False,
        )
        self._exit_thread = worker
        try:
            worker.start()
        except Exception as exc:
            self._exit_pending = False
            self._exit_thread = None
            self._logger.exception("Could not start desktop shutdown worker")
            if self._status_var is not None:
                self._status_var.set(f"Could not exit: {exc}")

    def _run_exit_request(self) -> None:
        try:
            result = self._on_exit()
        except Exception as exc:
            self._logger.exception("Desktop exit action failed")
            self._events.put(("exit_error", str(exc)))
            return
        self._events.put(("exit_complete", result is not False))

    def _hide_now(self) -> None:
        if self._root is not None:
            self._root.withdraw()


__all__ = [
    "Action",
    "CatalogLoader",
    "DirectorySelector",
    "DesktopApplication",
    "HistoryResetConfirmation",
    "MigrationAction",
    "MigrationConfirmation",
    "PathOpener",
    "StartupGetter",
    "StartupSetter",
    "format_package_size",
]
