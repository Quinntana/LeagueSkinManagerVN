"""Manual enemy cooldown panel, opened on demand from the tray.

This is the user-facing half of the cooldown-timer port from
LOL_Minimap_Tracker. The original PyQt5 panel was fed by live-game data;
this adaptation keeps the identical manual interaction model (click to
start/restart, right-click to cancel) but sources durations from fixed
summoner-spell presets and a per-row ultimate seconds entry, so it stays
free of network and game-client integrations.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .timer import (
    CooldownDefinition,
    CooldownKey,
    CooldownSlot,
    CooldownSnapshot,
    CooldownTimerStore,
)

ROLE_LABELS = ("Top", "Jungle", "Mid", "Bot", "Support")
DEFAULT_ULTIMATE_SECONDS = 120
MIN_MANUAL_SECONDS = 5
MAX_MANUAL_SECONDS = 3600


@dataclass(frozen=True)
class SpellPreset:
    """One selectable summoner spell with its flat base cooldown."""

    label: str
    identifier: str
    seconds: float


SUMMONER_SPELL_PRESETS: tuple[SpellPreset, ...] = (
    SpellPreset("Flash 300s", "SummonerFlash", 300.0),
    SpellPreset("Teleport 360s", "SummonerTeleport", 360.0),
    SpellPreset("Ignite 180s", "SummonerDot", 180.0),
    SpellPreset("Heal 240s", "SummonerHeal", 240.0),
    SpellPreset("Barrier 180s", "SummonerBarrier", 180.0),
    SpellPreset("Exhaust 210s", "SummonerExhaust", 210.0),
    SpellPreset("Cleanse 210s", "SummonerBoost", 210.0),
    SpellPreset("Ghost 210s", "SummonerHaste", 210.0),
    SpellPreset("Smite 90s", "SummonerSmite", 90.0),
)


def preset_by_label(label: str) -> SpellPreset | None:
    """Return the preset matching one combobox label, if any."""

    for preset in SUMMONER_SPELL_PRESETS:
        if preset.label == label:
            return preset
    return None


def manual_definition(identifier: str, display_name: str, seconds: float) -> CooldownDefinition:
    """Build a one-rank definition for a caller-chosen flat cooldown."""

    if (
        isinstance(seconds, bool)
        or not math.isfinite(seconds)
        or not MIN_MANUAL_SECONDS <= seconds <= MAX_MANUAL_SECONDS
    ):
        raise ValueError(f"seconds must be between {MIN_MANUAL_SECONDS} and {MAX_MANUAL_SECONDS}")
    return CooldownDefinition(
        identifier=identifier,
        display_name=display_name,
        icon_path=None,
        cooldowns=(float(seconds),),
        max_rank=1,
        unsupported_reason=None,
    )


def format_slot_text(base: str, snapshot: CooldownSnapshot | None) -> str:
    """Render one slot button caption from its current timer state."""

    if snapshot is None:
        return base
    if snapshot.is_ready:
        return f"{base} - ready"
    return f"{base} - {math.ceil(snapshot.remaining):d}s"


class CooldownBoard:
    """UI-free interaction model over the ported timer store.

    Rows are stable board positions (one per enemy role); champion names are
    free text captured at press time purely for the research event log.
    """

    def __init__(
        self,
        store: CooldownTimerStore,
        *,
        flush: Callable[[], None] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._store = store
        self._flush = flush
        self._logger = logger or logging.getLogger(__name__)

    @staticmethod
    def key_for(row: int, slot: CooldownSlot) -> CooldownKey:
        return CooldownKey(f"row-{row}", slot)

    def press(
        self,
        row: int,
        slot: CooldownSlot,
        *,
        identifier: str,
        display_name: str,
        seconds: float,
        champion: str,
    ) -> CooldownSnapshot | None:
        """Start or restart the timer behind one clicked slot."""

        definition = manual_definition(identifier, display_name, seconds)
        snapshot = self._store.start(
            self.key_for(row, slot),
            champion.strip() or ROLE_LABELS[row % len(ROLE_LABELS)],
            definition,
            1,
        )
        self._flush_events()
        return snapshot

    def clear(self, row: int, slot: CooldownSlot) -> bool:
        """Cancel the timer behind one right-clicked slot."""

        cleared = self._store.clear(self.key_for(row, slot))
        if cleared:
            self._flush_events()
        return cleared

    def clear_all(self) -> int:
        """Cancel every timer on the board."""

        cleared = self._store.clear_all()
        if cleared:
            self._flush_events()
        return cleared

    def text(self, row: int, slot: CooldownSlot, base: str) -> str:
        """Return the current caption for one slot button."""

        return format_slot_text(base, self._store.snapshot(self.key_for(row, slot)))

    def _flush_events(self) -> None:
        if self._flush is None:
            return
        try:
            self._flush()
        except OSError as error:
            self._logger.warning("Could not persist cooldown events: %s", error)


BACKGROUND = "#0b1220"
PANEL = "#121c30"
FOREGROUND = "#e8eefc"
MUTED = "#9fb0d0"
ACCENT = "#1f2f4d"


def apply_styles(ttk: Any, root: Any) -> None:
    """Register the dark ttk styles this window uses on its own root."""

    style = ttk.Style(root)
    with suppress(Exception):
        style.theme_use("clam")
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Panel.TLabel", background=PANEL, foreground=MUTED)
    style.configure(
        "Secondary.TButton",
        background=ACCENT,
        foreground=FOREGROUND,
        borderwidth=0,
        padding=(10, 5),
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#2b3f66"), ("disabled", "#16203a")],
        foreground=[("disabled", MUTED)],
    )


class CooldownWindow:
    """Standalone Tk window bound to a :class:`CooldownBoard`.

    It owns its own Tk root and event loop so it can be opened straight from
    the tray without any other window existing.  Closing it only hides it;
    running timers continue in the board's store until the app stops.
    """

    REFRESH_MILLISECONDS = 250

    def __init__(
        self,
        board: CooldownBoard,
        logger: logging.Logger | None = None,
    ) -> None:
        self._board = board
        self._logger = logger or logging.getLogger(__name__)
        self._champion_vars: list[Any] = []
        self._ultimate_vars: list[Any] = []
        self._spell_vars: dict[tuple[int, CooldownSlot], Any] = {}
        self._slot_buttons: dict[tuple[int, CooldownSlot], Any] = {}
        self._closing = False
        self._lock = Lock()
        self._window: Any | None = None

    def run(self) -> None:
        """Create the window and run its event loop on the calling thread.

        Tk requires the interpreter to be created and driven by the same
        thread, so construction deliberately happens here rather than in
        ``__init__`` - the host builds the object on the tray thread and runs
        it on its own.
        """

        import tkinter as tk
        from tkinter import ttk

        window: Any | None = None
        try:
            window = tk.Tk()
            with self._lock:
                self._window = window
            self._build(window, tk, ttk)
            self._refresh()
            window.mainloop()
        finally:
            self._closing = True
            with self._lock:
                self._window = None
            # Release every Tk variable and widget reference while still on the
            # thread that owns the interpreter. Letting them reach the garbage
            # collector afterwards finalizes them from another thread, which Tk
            # reports as "main thread is not in main loop" / Tcl_AsyncDelete.
            self._champion_vars.clear()
            self._ultimate_vars.clear()
            self._spell_vars.clear()
            self._slot_buttons.clear()
            if window is not None:
                with suppress(Exception):
                    window.destroy()

    def _build(self, window: Any, tk: Any, ttk: Any) -> None:
        window.title("Enemy cooldown timers")
        window.configure(background=BACKGROUND)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.hide)
        apply_styles(ttk, window)

        outer = ttk.Frame(window, style="Panel.TFrame", padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="Click starts or restarts a timer; right-click cancels it.",
            style="Panel.TLabel",
        ).grid(row=0, column=0, columnspan=5, sticky="w", pady=(0, 10))

        spell_labels = tuple(preset.label for preset in SUMMONER_SPELL_PRESETS)
        for index, role in enumerate(ROLE_LABELS):
            grid_row = index + 1
            champion_var = tk.StringVar(value=role)
            self._champion_vars.append(champion_var)
            ttk.Entry(outer, textvariable=champion_var, width=14).grid(
                row=grid_row, column=0, padx=(0, 8), pady=3, sticky="w"
            )

            ultimate_var = tk.StringVar(value=str(DEFAULT_ULTIMATE_SECONDS))
            self._ultimate_vars.append(ultimate_var)
            ttk.Spinbox(
                outer,
                from_=MIN_MANUAL_SECONDS,
                to=MAX_MANUAL_SECONDS,
                textvariable=ultimate_var,
                width=5,
            ).grid(row=grid_row, column=1, padx=(0, 4), pady=3)
            self._add_slot_button(outer, ttk, grid_row, 2, index, CooldownSlot.ULTIMATE, "Ult")

            for column, (slot, default_label) in enumerate(
                (
                    (CooldownSlot.SPELL_ONE, spell_labels[0]),
                    (CooldownSlot.SPELL_TWO, spell_labels[2]),
                ),
                start=3,
            ):
                spell_var = tk.StringVar(value=default_label)
                self._spell_vars[(index, slot)] = spell_var
                cell = ttk.Frame(outer, style="Panel.TFrame")
                cell.grid(row=grid_row, column=column, padx=(8, 0), pady=3, sticky="w")
                ttk.Combobox(
                    cell,
                    textvariable=spell_var,
                    values=spell_labels,
                    state="readonly",
                    width=13,
                ).pack(side="left", padx=(0, 4))
                self._add_slot_button(cell, ttk, None, None, index, slot, "Start")

        footer = ttk.Frame(outer, style="Panel.TFrame")
        footer.grid(row=len(ROLE_LABELS) + 1, column=0, columnspan=5, sticky="ew", pady=(12, 0))
        ttk.Button(
            footer,
            text="Reset all timers",
            style="Secondary.TButton",
            command=self._board.clear_all,
        ).pack(side="right")

    def show(self) -> None:
        """Raise the window from another thread, if it has been created yet."""

        window = self._active_window()
        if window is None:
            # Not built yet; ``run`` shows it as soon as it exists.
            return
        with suppress(Exception):
            window.after(0, self._show_now)

    def stop(self) -> None:
        """Ask the event loop to end so the hosting thread can join."""

        self._closing = True
        window = self._active_window()
        if window is None:
            return
        with suppress(Exception):
            window.after(0, window.quit)

    def hide(self) -> None:
        window = self._active_window()
        if window is not None:
            window.withdraw()

    def _active_window(self) -> Any | None:
        with self._lock:
            return self._window

    def _show_now(self) -> None:
        window = self._active_window()
        if window is None:
            return
        window.deiconify()
        window.lift()

    @property
    def exists(self) -> bool:
        window = self._active_window()
        if window is None:
            return False
        try:
            return bool(window.winfo_exists())
        except Exception:
            return False

    def _add_slot_button(
        self,
        parent: Any,
        ttk: Any,
        grid_row: int | None,
        grid_column: int | None,
        row: int,
        slot: CooldownSlot,
        base: str,
    ) -> None:
        button = ttk.Button(
            parent,
            text=base,
            style="Secondary.TButton",
            width=14,
            command=lambda: self._pressed(row, slot),
        )
        if grid_row is None:
            button.pack(side="left")
        else:
            button.grid(row=grid_row, column=grid_column, pady=3, sticky="w")
        button.bind("<Button-3>", lambda _event: self._board.clear(row, slot))
        self._slot_buttons[(row, slot)] = button

    def _pressed(self, row: int, slot: CooldownSlot) -> None:
        try:
            champion = str(self._champion_vars[row].get())
            if slot is CooldownSlot.ULTIMATE:
                seconds = float(str(self._ultimate_vars[row].get()).strip())
                self._board.press(
                    row,
                    slot,
                    identifier="ManualUltimate",
                    display_name="Ultimate",
                    seconds=seconds,
                    champion=champion,
                )
                return
            preset = preset_by_label(str(self._spell_vars[(row, slot)].get()))
            if preset is None:
                return
            self._board.press(
                row,
                slot,
                identifier=preset.identifier,
                display_name=preset.label,
                seconds=preset.seconds,
                champion=champion,
            )
        except ValueError as error:
            self._logger.warning("Cooldown timer was not started: %s", error)

    def _refresh(self) -> None:
        window = self._active_window()
        if self._closing or window is None or not self.exists:
            return
        for (row, slot), button in self._slot_buttons.items():
            base = "Ult" if slot is CooldownSlot.ULTIMATE else "Start"
            try:
                button.configure(text=self._board.text(row, slot, base))
            except Exception:
                self._logger.exception("Could not refresh a cooldown slot button")
                return
        window.after(self.REFRESH_MILLISECONDS, self._refresh)


def create_cooldown_window(
    board: CooldownBoard,
    logger: logging.Logger | None = None,
) -> CooldownWindow:
    """Build the window object; Tk itself is created when ``run`` is called."""

    return CooldownWindow(board, logger)


__all__ = [
    "BACKGROUND",
    "DEFAULT_ULTIMATE_SECONDS",
    "MAX_MANUAL_SECONDS",
    "MIN_MANUAL_SECONDS",
    "ROLE_LABELS",
    "SUMMONER_SPELL_PRESETS",
    "CooldownBoard",
    "CooldownWindow",
    "SpellPreset",
    "apply_styles",
    "create_cooldown_window",
    "format_slot_text",
    "manual_definition",
    "preset_by_label",
]
