"""The enemy cooldown board.

This package is the application's one enforced internal boundary.  Nothing
outside it may import its internals: the shell sees the six functions below
and nothing else.

The board's lifetime is the *match*, not the window. ``open_panel`` and
``close_panel`` show and hide one board whose timers keep running either way;
``release_panel`` is the only thing that tears it down, and the game process
exiting is the only thing that calls it. That is what keeps a second Tk
interpreter from ever existing in this process -- creating one aborts Tcl
outright, with no exception to catch.

The boundary is what lets the board depend on the Live Client API and Data
Dragon without any of that risk reaching the skin pipeline -- if this package
fails entirely, skins are unaffected, and the reverse holds too.

The panel runs on its own thread with its own Tk root, so a crash here cannot
take the tray's event loop with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from threading import Lock

from .host import WindowHost

LOGGER = logging.getLogger(__name__)

_lock = Lock()
_host: WindowHost | None = None
_window: object | None = None


def open_panel(
    *,
    cache_dir: Path | None = None,
    opacity: float = 0.85,
    scale: float = 1.0,
    left: int | None = None,
    top: int | None = None,
    opacity_choices: tuple[float, ...] = (0.85,),
    scale_choices: tuple[float, ...] = (1.0,),
    on_closed: Callable[[], None] | None = None,
    on_display: Callable[[float, float], None] | None = None,
    on_move: Callable[[int, int], None] | None = None,
    on_hidden: Callable[[bool], None] | None = None,
    logger: logging.Logger = LOGGER,
) -> bool:
    """Show the cooldown board, building the session if there is not one.

    A game has one set of enemy cooldowns, so there is one board per game. A
    second call while it is already on screen does nothing at all: the tray
    also greys its entry out, but that is a hint, and this is the guarantee.
    """

    global _host, _window

    from .board import CooldownBoard
    from .catalog import CooldownCatalog
    from .live import LiveClient
    from .panel import create_cooldown_window
    from .timer import CooldownTimerStore, SystemClock

    with _lock:
        if _host is not None and _host.is_running:
            window = _window
            if window is not None and getattr(window, "is_visible", False):
                return True
            return bool(_host.show())

        client = LiveClient(logger=logger.getChild("live"))
        catalog = CooldownCatalog(
            cache_dir or Path.home() / ".cache" / "lsmvn-cooldowns",
            logger=logger.getChild("catalog"),
        )

        def build() -> object:
            board = CooldownBoard(
                CooldownTimerStore(SystemClock(), None),
                roster=client.enemy_roster,
                resolve=catalog.loadouts,
                logger=logger.getChild("board"),
            )
            window = create_cooldown_window(
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
                logger=logger.getChild("window"),
            )
            global _window
            _window = window
            return window

        _host = WindowHost(
            build,  # type: ignore[arg-type]
            title="Enemy cooldowns",
            thread_name="cooldown-panel",
            logger=logger.getChild("host"),
        )
        return bool(_host.show())


def close_panel(timeout: float = 5.0) -> bool:
    """Hide the board. The session keeps running and timers keep counting.

    ``timeout`` is accepted so the signature is unchanged for callers, and
    ignored: nothing is being waited on.
    """

    del timeout
    with _lock:
        window = _window
        if window is None:
            return True
        hide = getattr(window, "hide", None)
        if not callable(hide):
            return False
    try:
        hide()
    except Exception:  # noqa: BLE001 - hiding must never raise into the tray
        LOGGER.debug("Could not hide the cooldown board", exc_info=True)
        return False
    return True


def release_panel(timeout: float = 5.0) -> bool:
    """End the session: stop the board and free everything it owns.

    Called when the game process exits. This is the only path that tears the
    interpreter down, so it happens once per match rather than once per time
    the player glances at the board.
    """

    global _host, _window

    with _lock:
        host, _host = _host, None
        _window = None
        if host is None:
            return True
        return bool(host.stop(timeout))


def is_visible() -> bool:
    """Whether the board is on screen, as opposed to merely existing."""

    with _lock:
        window = _window
        host = _host
    if host is None or not host.is_running or window is None:
        return False
    return bool(getattr(window, "is_visible", False))


def is_open() -> bool:
    with _lock:
        return _host is not None and _host.is_running


def apply_display(opacity: float, scale: float) -> bool:
    """Apply display settings to a live panel; a no-op when there is no session.

    The board carries its own opacity and scale controls, because both are
    judged against what is on screen. The tray offers the same two so they stay
    reachable while the board is hidden or absent.
    """

    with _lock:
        window = _window
        host = _host
    if host is None or not host.is_running or window is None:
        return False
    setter = getattr(window, "set_display", None)
    if not callable(setter):
        return False
    try:
        setter(opacity=opacity, scale=scale)
    except Exception:  # noqa: BLE001 - display settings are cosmetic
        LOGGER.debug("Could not apply cooldown display settings", exc_info=True)
        return False
    return True


__all__ = [
    "apply_display",
    "close_panel",
    "is_open",
    "is_visible",
    "open_panel",
    "release_panel",
]
