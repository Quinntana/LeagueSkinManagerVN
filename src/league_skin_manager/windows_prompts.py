"""Small, explicit Windows prompt adapters kept outside core services."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

DirectorySelector = Callable[[Path], Path | None]
MigrationConfirmer = Callable[[Path], bool]


def prompt_for_ltk_migration_source(
    initial_dir: Path,
    *,
    selector: DirectorySelector | None = None,
    confirmer: MigrationConfirmer | None = None,
) -> Path | None:
    """Choose and confirm a source only after the user clicks the tray port action."""

    if (selector is None) != (confirmer is None):
        raise ValueError("selector and confirmer must be provided together")
    if selector is not None and confirmer is not None:
        source = selector(initial_dir)
        return source if source is not None and confirmer(source) else None

    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.withdraw()
    try:
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(
            parent=root,
            title="Choose CSLOL Manager folder or its installed folder",
            initialdir=str(initial_dir),
            mustexist=True,
        )
        if not selected:
            return None
        source = Path(selected)
        confirmed = messagebox.askyesno(
            "Port CSLOL skins to LTK now",
            (
                f"Source: {source}\n\n"
                "Only after you confirm, LeagueSkinManagerVN will validate and queue these "
                "mods in LTK's supported archive inbox. CSLOL originals are left unchanged.\n\n"
                "Close both CSLOL Manager and LTK Manager before continuing. Existing and "
                "previously queued content is detected by SHA-256 and skipped. Continue?"
            ),
            default=messagebox.NO,
            parent=root,
        )
        return source if confirmed else None
    finally:
        root.destroy()


__all__ = [
    "DirectorySelector",
    "MigrationConfirmer",
    "prompt_for_ltk_migration_source",
]
