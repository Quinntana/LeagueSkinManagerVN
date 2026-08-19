"""The enemy cooldown window.

Layout inherited from LOL_Minimap_Tracker's Qt panel, redrawn in Tk: one
compact row per enemy, each holding a champion cell and three squares for the
ultimate and both summoner spells.  It is deliberately small -- it sits over a
live game -- so opacity and scale are chosen from the tray rather than from
controls that would cost screen space here.

Left click only. Idle -> counting -> cancel -> counting again, as a fresh
timer rather than a resumed one.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from threading import Lock
from typing import Any

from .board import MAX_ROWS, SLOTS, CooldownBoard, RowView

LOGGER = logging.getLogger(__name__)

# Palette carried over from the Qt panel so the board looks the same.
BACKGROUND = "#0b1220"
SURFACE = "#111827"
BORDER = "#5d6980"
FOREGROUND = "#e8eefc"
MUTED = "#bcc4d3"
READY = "#4ade80"
DISABLED = "#39415a"

CELL = 34
ICON = 30
ROW_HEIGHT = 38
REFRESH_MILLISECONDS = 250
ROSTER_SECONDS = 5.0


class CooldownWindow:
    """A standalone Tk window bound to a :class:`CooldownBoard`.

    It owns its own Tk root and event loop so it can open straight from the
    tray with no other window in existence, and so a failure here cannot reach
    the tray's loop.
    """

    def __init__(
        self,
        board: CooldownBoard,
        *,
        opacity: float = 0.85,
        scale: float = 1.0,
        on_closed: Any = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._board = board
        self._opacity = opacity
        self._scale = scale
        self._on_closed = on_closed
        self._logger = logger
        self._lock = Lock()
        self._window: Any | None = None
        self._closing = False
        self._cells: list[dict[str, Any]] = []
        self._ticks = 0

    # -- lifecycle -------------------------------------------------------

    def run(self) -> None:
        """Build and drive the window on the calling thread.

        Tk requires the interpreter to be created and driven by one thread, so
        construction happens here rather than in ``__init__``.
        """

        import tkinter as tk

        window: Any | None = None
        try:
            window = tk.Tk()
            with self._lock:
                self._window = window
            self._build(window, tk)
            self._refresh()
            window.mainloop()
        except Exception:  # noqa: BLE001 - never propagate into the host thread
            self._logger.exception("The cooldown window failed")
        finally:
            self._closing = True
            with self._lock:
                self._window = None
            # Drop widget references while still on the thread that owns the
            # interpreter; finalizing them elsewhere makes Tk complain about
            # "main thread is not in main loop".
            self._cells.clear()
            if window is not None:
                with suppress(Exception):
                    window.destroy()
            self._notify_closed()

    def show(self) -> None:
        window = self._window
        if window is None:
            return
        with suppress(Exception):
            window.deiconify()
            window.lift()

    def stop(self) -> None:
        self._closing = True
        window = self._window
        if window is not None:
            with suppress(Exception):
                window.after(0, window.quit)

    def hide(self) -> None:
        """The window's close button: stop, so the app can suppress re-opening."""

        self.stop()

    def set_display(self, *, opacity: float, scale: float) -> None:
        """Apply tray-chosen display settings to a live window."""

        self._opacity = opacity
        self._scale = scale
        window = self._window
        if window is None:
            return
        with suppress(Exception):
            window.after(0, self._apply_display)

    def _apply_display(self) -> None:
        window = self._window
        if window is None:
            return
        with suppress(Exception):
            window.attributes("-alpha", max(0.2, min(1.0, self._opacity)))
        with suppress(Exception):
            window.tk.call("tk", "scaling", max(0.5, min(3.0, self._scale)) * 1.3333)

    # -- construction ----------------------------------------------------

    def _build(self, window: Any, tk: Any) -> None:
        window.title("Enemy cooldowns")
        window.configure(background=BACKGROUND)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.hide)
        with suppress(Exception):
            window.attributes("-topmost", True)
        self._apply_display()

        outer = tk.Frame(window, background=BACKGROUND, padx=6, pady=6)
        outer.pack(fill="both", expand=True)

        for row in range(MAX_ROWS):
            line = tk.Frame(outer, background=BACKGROUND, height=ROW_HEIGHT)
            line.pack(fill="x", pady=1)

            champion = tk.Label(
                line,
                text="?",
                width=9,
                anchor="w",
                background=SURFACE,
                foreground=MUTED,
                font=("Segoe UI", 8),
                padx=6,
                pady=4,
            )
            champion.pack(side="left", padx=(0, 4))

            buttons: dict[Any, Any] = {}
            for slot in SLOTS:
                button = tk.Label(
                    line,
                    text="-",
                    width=4,
                    background=SURFACE,
                    foreground=MUTED,
                    font=("Segoe UI Semibold", 9),
                    pady=4,
                    highlightthickness=1,
                    highlightbackground=BORDER,
                )
                button.pack(side="left", padx=1)
                button.bind(
                    "<Button-1>",
                    lambda _event, r=row, s=slot: self._pressed(r, s),
                )
                buttons[slot] = button
            self._cells.append({"champion": champion, "buttons": buttons})

    # -- interaction -----------------------------------------------------

    def _pressed(self, row: int, slot: Any) -> None:
        try:
            self._board.press(row, slot)
        except Exception:  # noqa: BLE001 - a click must never kill the loop
            self._logger.exception("Cooldown press failed")
        self._paint()

    # -- refresh ---------------------------------------------------------

    def _refresh(self) -> None:
        if self._closing:
            return
        window = self._window
        if window is None:
            return

        # Roster polling is far slower than repainting: identities change once
        # a match, remaining seconds change four times a second.
        self._ticks += 1
        if self._ticks % max(1, int(ROSTER_SECONDS * 1000 / REFRESH_MILLISECONDS)) == 1:
            self._board.refresh()

        self._paint()
        with suppress(Exception):
            window.after(REFRESH_MILLISECONDS, self._refresh)

    def _paint(self) -> None:
        try:
            rows = self._board.rows()
        except Exception:  # noqa: BLE001
            self._logger.exception("Could not read the cooldown board")
            return
        for index, view in enumerate(rows[: len(self._cells)]):
            self._paint_row(self._cells[index], view)

    def _paint_row(self, cell: dict[str, Any], view: RowView) -> None:
        label = view.champion or "?"
        if view.level is not None:
            label = f"{label} {view.level}"
        with suppress(Exception):
            cell["champion"].configure(
                text=label[:12],
                foreground=MUTED if view.is_placeholder else FOREGROUND,
            )

        for slot_view in view.slots:
            button = cell["buttons"].get(slot_view.slot)
            if button is None:
                continue
            if slot_view.counting:
                foreground, border = FOREGROUND, BORDER
            elif slot_view.is_ready:
                foreground, border = READY, READY
            elif slot_view.enabled:
                foreground, border = MUTED, BORDER
            else:
                foreground, border = DISABLED, DISABLED
            with suppress(Exception):
                button.configure(
                    text=slot_view.caption,
                    foreground=foreground,
                    highlightbackground=border,
                    cursor="hand2" if slot_view.enabled else "arrow",
                )

    def _notify_closed(self) -> None:
        if self._on_closed is None:
            return
        try:
            self._on_closed()
        except Exception:  # noqa: BLE001
            self._logger.debug("Cooldown close observer failed", exc_info=True)


def create_cooldown_window(
    board: CooldownBoard,
    *,
    opacity: float = 0.85,
    scale: float = 1.0,
    on_closed: Any = None,
    logger: logging.Logger = LOGGER,
) -> CooldownWindow:
    """Build the window object; Tk itself is created when ``run`` is called."""

    return CooldownWindow(board, opacity=opacity, scale=scale, on_closed=on_closed, logger=logger)


__all__ = [
    "BACKGROUND",
    "READY",
    "REFRESH_MILLISECONDS",
    "ROSTER_SECONDS",
    "CooldownWindow",
    "create_cooldown_window",
]
