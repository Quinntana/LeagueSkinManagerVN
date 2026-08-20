"""The enemy cooldown overlay.

A borderless, always-on-top, non-activating window that sits over a Borderless
or Windowed League client. It cannot draw over exclusive fullscreen -- nothing
can, short of hooking DirectX -- which is a documented limitation rather than a
bug.

The layout is one compact row per enemy: the champion's portrait, then three
slots for the ultimate and both summoner spells. Champion level is read from
the live client to pick the right cooldown rank, but is never drawn; the
scoreboard already shows it and this surface exists to be small.

Slots are Canvases rather than Labels because window opacity is uniform and
cannot fade the background while leaving the number solid. A counting slot
therefore darkens its own icon with a stipple pattern and draws the remaining
seconds outlined in black over white, so state stays legible at every opacity.

Left click drives timers and drags the board. Right click does nothing
anywhere: it is how a player moves, so a stray one must cost nothing.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from pathlib import Path
from threading import Lock
from typing import Any

from ..config import LEAGUE_GAME_PROCESS_NAME
from .board import MAX_ROWS, SLOTS, CooldownBoard, RosterPoller, RowView, SlotView
from .overlay import OverlayWindow, enable_dpi_awareness

LOGGER = logging.getLogger(__name__)

BACKGROUND = "#0b1220"
SURFACE = "#111827"
BORDER = "#5d6980"
FOREGROUND = "#e8eefc"
MUTED = "#bcc4d3"
READY = "#4ade80"
CONTROL = "#6b7688"
DISABLED = "#39415a"
UNAVAILABLE = "#ef4444"
INK = "#000000"
PAPER = "#ffffff"

BASE_ICON = 32
BASE_PAD = 3
BASE_SMALL_FONT = 8
BASE_CELL_FONT = 11

REFRESH_MILLISECONDS = 250
DRAG_THRESHOLD = 4

# The eight offsets that fake a text outline. Tk cannot stroke text, and this
# is what keeps the number readable against bright ability art.
_OUTLINE = ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1), (-1, 1), (1, -1))


class CooldownWindow:
    """A standalone Tk overlay bound to a :class:`CooldownBoard`.

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
        left: int | None = None,
        top: int | None = None,
        opacity_choices: tuple[float, ...] = (0.85,),
        scale_choices: tuple[float, ...] = (1.0,),
        on_closed: Any = None,
        on_display: Any = None,
        on_move: Any = None,
        on_hidden: Any = None,
        overlay: OverlayWindow | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._board = board
        self._opacity = opacity
        self._scale = scale
        self._left = left
        self._top = top
        self._opacities = opacity_choices or (opacity,)
        self._scales = scale_choices or (scale,)
        self._on_closed = on_closed
        self._on_display = on_display
        self._on_move = on_move
        self._on_hidden = on_hidden
        self._overlay = overlay if overlay is not None else OverlayWindow(logger=logger)
        self._logger = logger
        self._lock = Lock()
        self._window: Any | None = None
        self._body: Any | None = None
        self._readout: Any | None = None
        self._closing = False
        self._rows: list[dict[str, Any]] = []
        self._images: dict[tuple[str, int], Any] = {}
        self._poller: RosterPoller | None = None
        self._ticks = 0
        self._anchor = 0.0
        # Two independent reasons to be off screen. The foreground watcher may
        # only ever clear its own, which is what stops alt-tabbing back into
        # the game from undoing a hide the user asked for.
        self._hidden_by_user = False
        self._hidden_for_foreground = False
        self._drag: dict[str, Any] = {"x": 0, "y": 0, "ox": 0, "oy": 0, "moved": False}

    # -- lifecycle -------------------------------------------------------

    def run(self) -> None:
        """Build and drive the window on the calling thread."""

        import time
        import tkinter as tk

        window: Any | None = None
        try:
            enable_dpi_awareness()
            window = tk.Tk()
            with self._lock:
                self._window = window
            self._build(window, tk)
            self._poller = RosterPoller(self._board, logger=self._logger.getChild("roster"))
            self._poller.start()
            self._anchor = time.monotonic()
            self._refresh()
            window.mainloop()
        except Exception:  # noqa: BLE001 - never propagate into the host thread
            self._logger.exception("The cooldown window failed")
        finally:
            self._closing = True
            if self._poller is not None:
                self._poller.stop()
                self._poller = None
            self._release()
            if window is not None:
                with suppress(Exception):
                    window.destroy()
            window = None
            self._notify_closed()

    def _release(self) -> None:
        """Drop every reference to the interpreter, on the thread that owns it.

        This is not tidiness. Tcl aborts the whole process with
        ``Tcl_AsyncDelete: async handler deleted by the wrong thread`` if the
        last reference to a Tk object is released on a thread other than the
        one that created it -- and it aborts, so there is no exception to catch
        and nothing reaches the log.

        Every widget holds its interpreter, so a single surviving widget
        attribute is enough to move that finalisation onto whichever thread
        later drops this object. ``close_panel`` runs on the tray thread, which
        is exactly that situation.
        """

        with self._lock:
            self._window = None
        self._body = None
        self._readout = None
        self._rows.clear()
        self._images.clear()

    @property
    def is_visible(self) -> bool:
        """Whether the board is actually on screen.

        Distinct from the session existing: a hidden board is still counting.
        """

        return (
            self._window is not None
            and not self._hidden_by_user
            and not self._hidden_for_foreground
        )

    def show(self) -> None:
        """Show the board, clearing a deliberate hide."""

        self._set_hidden_by_user(False)

    def hide(self) -> None:
        """The close control. Hides; it does not end the session.

        Timers keep running, so the player gets them back exactly where they
        were rather than losing what they were tracking.
        """

        self._set_hidden_by_user(True)

    def _set_hidden_by_user(self, hidden: bool) -> None:
        """Record the user's intent, then reconcile on the interpreter's thread.

        ``show`` and ``hide`` are called from the tray's thread. Touching
        widgets from there is exactly the cross-thread use Tk does not support,
        so the window work is marshalled with ``after`` -- the same reason
        ``set_display`` does it.
        """

        changed = hidden != self._hidden_by_user
        self._hidden_by_user = hidden
        window = self._window
        if window is not None:
            with suppress(Exception):
                window.after(0, self._apply_visibility)
        if changed and self._on_hidden is not None:
            # Deliberate intent only. Hiding because the game lost focus is
            # transient and must not make the tray's entry flicker.
            try:
                self._on_hidden(hidden)
            except Exception:  # noqa: BLE001 - an observer must not break hiding
                self._logger.debug("Cooldown visibility observer failed", exc_info=True)

    def stop(self) -> None:
        """End the session: leave the loop so ``run`` can tear everything down."""

        self._closing = True
        window = self._window
        if window is not None:
            with suppress(Exception):
                window.after(0, window.quit)

    def _apply_visibility(self) -> None:
        """Reconcile the window with the two independent hidden states."""

        window = self._window
        if window is None:
            return
        with suppress(Exception):
            if self.is_visible:
                window.deiconify()
                window.attributes("-topmost", True)
                window.lift()
                # Repaint at once rather than waiting for the next tick, so the
                # board never appears holding stale numbers. Nothing was drawn
                # while it was hidden, so the cached state has to be discarded
                # or the skip-if-unchanged check would keep it blank.
                self._invalidate()
                self._paint()
            else:
                window.withdraw()

    def _invalidate(self) -> None:
        """Force the next paint to redraw everything."""

        for cell in self._rows:
            cell.pop("face_state", None)
            for canvas in cell["slots"].values():
                with suppress(Exception):
                    canvas.paint_state = None

    def set_display(self, *, opacity: float, scale: float) -> None:
        """Apply tray-chosen display settings to a live window."""

        rebuild = scale != self._scale
        self._opacity = opacity
        self._scale = scale
        window = self._window
        if window is None:
            return
        with suppress(Exception):
            window.after(0, self._rebuild if rebuild else self._apply_opacity)

    # -- geometry --------------------------------------------------------

    def _metrics(self) -> dict[str, int]:
        scale = max(0.5, min(3.0, self._scale))
        return {
            "icon": max(14, round(BASE_ICON * scale)),
            "pad": max(1, round(BASE_PAD * scale)),
            "small": max(6, round(BASE_SMALL_FONT * scale)),
            "cell": max(7, round(BASE_CELL_FONT * scale)),
        }

    def _apply_opacity(self) -> None:
        window = self._window
        if window is None:
            return
        with suppress(Exception):
            window.attributes("-alpha", max(0.15, min(1.0, self._opacity)))

    def _clamped(self, left: int, top: int) -> tuple[int, int]:
        window = self._window
        if window is None:
            return left, top
        try:
            width = max(1, window.winfo_width())
            height = max(1, window.winfo_height())
            screen_width = window.winfo_screenwidth()
            screen_height = window.winfo_screenheight()
        except Exception:  # noqa: BLE001
            return left, top
        return (
            max(0, min(left, max(0, screen_width - width))),
            max(0, min(top, max(0, screen_height - height))),
        )

    # -- construction ----------------------------------------------------

    def _build(self, window: Any, tk: Any) -> None:
        window.title("Enemy cooldowns")
        window.configure(background=BACKGROUND)
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", self.hide)
        with suppress(Exception):
            window.overrideredirect(True)
        with suppress(Exception):
            window.attributes("-topmost", True)
        self._apply_opacity()

        self._compose(window, tk)

        window.update_idletasks()
        if self._left is not None and self._top is not None:
            left, top = self._clamped(self._left, self._top)
            with suppress(Exception):
                window.geometry(f"+{left}+{top}")
        # The style has to be applied after the window exists; winfo_id gives a
        # child handle, which OverlayWindow resolves to its top-level owner.
        with suppress(Exception):
            self._overlay.apply(int(window.winfo_id()))
        window.bind("<Button-3>", lambda _event: "break")

    def _rebuild(self) -> None:
        """Re-lay the board at a new scale, keeping the window and timers."""

        window = self._window
        if window is None:
            return
        import tkinter as tk

        with suppress(Exception):
            self._apply_opacity()
            self._images.clear()
            self._rows.clear()
            if self._body is not None:
                self._body.destroy()
            self._compose(window, tk)
            self._paint()

    def _compose(self, window: Any, tk: Any) -> None:
        metrics = self._metrics()
        body = tk.Frame(window, background=BACKGROUND, padx=metrics["pad"], pady=metrics["pad"])
        body.pack(fill="both", expand=True)
        self._body = body

        head = tk.Frame(body, background=BACKGROUND)
        head.pack(fill="x")
        self._readout = tk.Label(
            head,
            text=self._readout_text(),
            background=BACKGROUND,
            foreground=CONTROL,
            font=("Segoe UI", max(6, metrics["small"] - 1)),
        )
        self._readout.pack(side="left")

        draggable = [body, head, self._readout]
        for text, action, colour in (
            ("x", self.hide, MUTED),
            ("S", self._cycle_scale, CONTROL),
            ("O", self._cycle_opacity, CONTROL),
        ):
            control = tk.Label(
                head,
                text=text,
                background=BACKGROUND,
                foreground=colour,
                font=("Segoe UI", metrics["small"]),
                padx=metrics["pad"],
                cursor="hand2",
            )
            control.pack(side="right")
            control.bind("<Button-1>", lambda _event, run=action: run())
            control.bind("<Button-3>", lambda _event: "break")

        for row in range(MAX_ROWS):
            line = tk.Frame(body, background=BACKGROUND)
            line.pack(fill="x", pady=1)
            face = tk.Label(
                line,
                background=SURFACE,
                foreground=MUTED,
                font=("Segoe UI", metrics["small"]),
                width=4,
            )
            face.pack(side="left", padx=(0, metrics["pad"]))

            slots: dict[Any, Any] = {}
            for slot in SLOTS:
                canvas = tk.Canvas(
                    line,
                    width=metrics["icon"],
                    height=metrics["icon"],
                    background=SURFACE,
                    highlightthickness=1,
                    highlightbackground=BORDER,
                    borderwidth=0,
                    cursor="hand2",
                )
                canvas.pack(side="left", padx=1)
                canvas.bind("<Button-1>", lambda _event, r=row, s=slot: self._pressed(r, s))
                canvas.bind("<Button-3>", lambda _event: "break")
                slots[slot] = canvas
            self._rows.append({"face": face, "slots": slots, "line": line})
            draggable.extend((line, face))

        for widget in draggable:
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)
            widget.bind("<ButtonRelease-1>", self._drag_end)

    def _readout_text(self) -> str:
        return f"{self._opacity:.2f}  {self._scale:g}x"

    # -- controls --------------------------------------------------------

    def _cycle_opacity(self) -> None:
        self._opacity = _next(self._opacities, self._opacity)
        self._apply_opacity()
        self._update_readout()
        self._publish_display()

    def _cycle_scale(self) -> None:
        self._scale = _next(self._scales, self._scale)
        self._rebuild()
        self._publish_display()

    def _update_readout(self) -> None:
        if self._readout is None:
            return
        with suppress(Exception):
            self._readout.configure(text=self._readout_text())

    def _publish_display(self) -> None:
        if self._on_display is None:
            return
        try:
            self._on_display(self._opacity, self._scale)
        except Exception:  # noqa: BLE001 - display settings are cosmetic
            self._logger.debug("Could not persist display settings", exc_info=True)

    # -- dragging --------------------------------------------------------

    def _drag_start(self, event: Any) -> None:
        window = self._window
        if window is None:
            return
        self._drag = {
            "x": event.x_root - window.winfo_x(),
            "y": event.y_root - window.winfo_y(),
            "ox": event.x_root,
            "oy": event.y_root,
            "moved": False,
        }

    def _drag_move(self, event: Any) -> None:
        window = self._window
        if window is None:
            return
        travelled = abs(event.x_root - self._drag["ox"]) + abs(event.y_root - self._drag["oy"])
        if travelled > DRAG_THRESHOLD:
            self._drag["moved"] = True
        if not self._drag["moved"]:
            return
        with suppress(Exception):
            left, top = self._clamped(
                event.x_root - self._drag["x"], event.y_root - self._drag["y"]
            )
            window.geometry(f"+{left}+{top}")

    def _drag_end(self, _event: Any) -> None:
        """Persist only a real move.

        Without the threshold every click -- including the close and display
        controls -- would rewrite the stored position, because release
        propagates up to the containers that carry these bindings.
        """

        window = self._window
        if window is None or not self._drag["moved"]:
            return
        self._drag["moved"] = False
        with suppress(Exception):
            self._left, self._top = window.winfo_x(), window.winfo_y()
        if self._on_move is None:
            return
        try:
            self._on_move(self._left, self._top)
        except Exception:  # noqa: BLE001 - a stored position is not critical
            self._logger.debug("Could not persist the board position", exc_info=True)

    # -- interaction -----------------------------------------------------

    def _pressed(self, row: int, slot: Any) -> None:
        try:
            self._board.press(row, slot)
        except Exception:  # noqa: BLE001 - a click must never kill the loop
            self._logger.exception("Cooldown press failed")
        self._paint()

    # -- refresh ---------------------------------------------------------

    def _refresh(self) -> None:
        import time

        if self._closing:
            return
        window = self._window
        if window is None:
            return

        self._follow_foreground()
        self._paint()

        # A fixed grid anchored at start, not a delay chained from the end of
        # the previous repaint: chaining accumulates the cost of every tick and
        # measurably ran 2.7% slow.
        self._ticks += 1
        target = self._anchor + self._ticks * (REFRESH_MILLISECONDS / 1000.0)
        delay = max(1, int((target - time.monotonic()) * 1000))
        with suppress(Exception):
            window.after(delay, self._refresh)

    def _follow_foreground(self) -> None:
        """Show the board only while the game is in front.

        ``None`` means the question could not be answered, and is deliberately
        not treated as "the game is gone".
        """

        window = self._window
        if window is None:
            return
        foreground = self._overlay.is_foreground(LEAGUE_GAME_PROCESS_NAME)
        if foreground is None:
            return
        # Only ever touches its own flag: a board the user hid stays hidden
        # when they come back to the game.
        hidden = not foreground
        if hidden != self._hidden_for_foreground:
            self._hidden_for_foreground = hidden
            self._apply_visibility()

    def _paint(self) -> None:
        """Redraw the rows. Does nothing while the board is off screen.

        Timers are read from a monotonic clock on demand, so there is nothing
        to accumulate while hidden -- the next visible paint reads the true
        remaining time with no catching up to do.
        """

        if not self.is_visible:
            return
        try:
            rows = self._board.rows()
        except Exception:  # noqa: BLE001
            self._logger.exception("Could not read the cooldown board")
            return
        for index, view in enumerate(rows[: len(self._rows)]):
            self._paint_row(self._rows[index], view)

    def _paint_row(self, cell: dict[str, Any], view: RowView) -> None:
        metrics = self._metrics()

        # Redraw only what changed. The board sits on a translucent layered
        # window, and Windows re-blends the whole window whenever any part of
        # it is repainted -- so redrawing all fifteen slots four times a second
        # flickers, most visibly on a click, which adds a repaint of its own on
        # top of the tick's.
        face_state = (view.champion, str(view.champion_icon_path), metrics["icon"])
        if cell.get("face_state") != face_state:
            cell["face_state"] = face_state
            portrait = self._image(view.champion_icon_path, metrics["icon"])
            with suppress(Exception):
                if portrait is not None:
                    cell["face"].configure(image=portrait, text="", width=0)
                else:
                    cell["face"].configure(
                        image="",
                        text=(view.champion or "?")[:4],
                        width=4,
                        foreground=MUTED if view.is_placeholder else FOREGROUND,
                    )

        for slot_view in view.slots:
            canvas = cell["slots"].get(slot_view.slot)
            if canvas is None:
                continue
            state = (
                slot_view.caption,
                slot_view.is_ready,
                slot_view.enabled,
                slot_view.counting,
                str(slot_view.icon_path),
                metrics["icon"],
            )
            if getattr(canvas, "paint_state", None) == state:
                continue
            canvas.paint_state = state
            self._paint_slot(canvas, slot_view, metrics)

    def _paint_slot(self, canvas: Any, view: SlotView, metrics: dict[str, int]) -> None:
        with suppress(Exception):
            canvas.delete("all")
            icon = metrics["icon"]
            middle = icon // 2 + 1
            art = self._image(view.icon_path, icon)
            counting = view.counting

            if art is not None:
                canvas.create_image(middle, middle, image=art)
            if art is not None and counting:
                # gray50 is Tk's 50% dither: fake per-pixel transparency with
                # no compositor and no imaging library.
                canvas.create_rectangle(
                    0, 0, icon + 2, icon + 2, fill=INK, stipple="gray50", outline=""
                )
            if not view.enabled and not counting:
                # A cross rather than a heavier dither. Darkening far enough to
                # read as unavailable also destroys the icon, and the icon is
                # how the two summoner slots are told apart.
                self._draw_cross(canvas, icon, metrics)

            if view.is_ready:
                canvas.configure(highlightbackground=READY)
                canvas.create_text(
                    middle,
                    middle,
                    text="up",
                    fill=READY,
                    font=("Segoe UI Semibold", metrics["cell"]),
                )
                return

            canvas.configure(highlightbackground=BORDER if view.enabled else DISABLED)
            if not counting:
                if art is None:
                    canvas.create_text(
                        middle,
                        middle,
                        text=view.caption,
                        fill=MUTED if view.enabled else DISABLED,
                        font=("Segoe UI Semibold", metrics["cell"]),
                    )
                return

            font = ("Segoe UI Black", metrics["cell"])
            for dx, dy in _OUTLINE:
                canvas.create_text(middle + dx, middle + dy, text=view.caption, fill=INK, font=font)
            canvas.create_text(middle, middle, text=view.caption, fill=PAPER, font=font)

    @staticmethod
    def _draw_cross(canvas: Any, icon: int, metrics: dict[str, int]) -> None:
        """Mark a slot unavailable without destroying its icon.

        Drawn dark first, then red on top, so it stays visible over both the
        bright and the dark parts of an ability icon.
        """

        inset = max(2, icon // 6)
        near, far = inset, icon + 2 - inset
        width = max(2, round(icon / 14))
        for colour, extra in ((INK, 2), (UNAVAILABLE, 0)):
            for start, end in (((near, near), (far, far)), ((far, near), (near, far))):
                canvas.create_line(
                    start[0], start[1], end[0], end[1], fill=colour, width=width + extra
                )

    def _image(self, path: Path | None, size: int) -> Any | None:
        """Load and cache a Tk image, sized by integer subsampling.

        Tk cannot interpolate, so an icon snaps to the nearest whole division
        of its native size. References must be held for the window's lifetime
        or Tk collects the image and draws nothing.
        """

        if path is None:
            return None
        window = self._window
        if window is None:
            return None
        key = (str(path), size)
        if key in self._images:
            return self._images[key]
        import tkinter as tk

        # Tk's image creation is occasionally, transiently unhappy: an
        # identical call issued immediately after a failure succeeds. One
        # retry, then give up for this repaint.
        for attempt in (1, 2):
            try:
                # Bind to this window's interpreter rather than Tk's implicit
                # default root, which belongs to whichever Tk was made first.
                raw = tk.PhotoImage(master=window, file=str(path))
                factor = max(1, round(raw.width() / size)) if size else 1
                image = raw if factor == 1 else raw.subsample(factor, factor)
            except Exception:  # noqa: BLE001 - a bad icon falls back to text
                self._logger.debug(
                    "Could not load the icon %s (attempt %d)", path, attempt, exc_info=True
                )
                continue
            self._images[key] = image
            return image

        # Deliberately not cached. A failure here may be transient, and
        # caching None would leave that square blank for the whole match.
        return None

    def _notify_closed(self) -> None:
        if self._on_closed is None:
            return
        try:
            self._on_closed()
        except Exception:  # noqa: BLE001
            self._logger.debug("Cooldown close observer failed", exc_info=True)


def _next(choices: tuple[float, ...], current: float) -> float:
    """The next preset after ``current``, wrapping, tolerant of a stale value."""

    if not choices:
        return current
    for index, value in enumerate(choices):
        if abs(value - current) < 1e-9:
            return choices[(index + 1) % len(choices)]
    return choices[0]


def create_cooldown_window(
    board: CooldownBoard,
    *,
    opacity: float = 0.85,
    scale: float = 1.0,
    left: int | None = None,
    top: int | None = None,
    opacity_choices: tuple[float, ...] = (0.85,),
    scale_choices: tuple[float, ...] = (1.0,),
    on_closed: Any = None,
    on_display: Any = None,
    on_move: Any = None,
    on_hidden: Any = None,
    logger: logging.Logger = LOGGER,
) -> CooldownWindow:
    """Build the window object; Tk itself is created when ``run`` is called."""

    return CooldownWindow(
        board,
        opacity=opacity,
        scale=scale,
        left=left,
        top=top,
        opacity_choices=opacity_choices,
        scale_choices=scale_choices,
        on_closed=on_closed,
        on_display=on_display,
        on_move=on_move,
        on_hidden=on_hidden,
        logger=logger,
    )


__all__ = [
    "BACKGROUND",
    "DRAG_THRESHOLD",
    "READY",
    "REFRESH_MILLISECONDS",
    "CooldownWindow",
    "create_cooldown_window",
]
