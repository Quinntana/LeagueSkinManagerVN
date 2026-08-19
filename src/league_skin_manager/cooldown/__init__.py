"""The enemy cooldown board.

This package is the application's one enforced internal boundary.  Nothing
outside it may import its internals: the shell sees the four functions below
and nothing else.  That isolation is deliberate, and it is what lets the board
depend on the Live Client API and Data Dragon without any of that risk
reaching the skin pipeline -- if this package fails entirely, skins are
unaffected, and the reverse holds too.

The panel runs on its own thread with its own Tk root, so a crash here cannot
take the tray's event loop with it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock

from .host import WindowHost

LOGGER = logging.getLogger(__name__)

_lock = Lock()
_host: WindowHost | None = None
_on_closed: Callable[[], None] | None = None


def open_panel(
    *,
    opacity: float = 0.85,
    scale: float = 1.0,
    on_closed: Callable[[], None] | None = None,
    logger: logging.Logger = LOGGER,
) -> bool:
    """Show the cooldown board. Returns whether it is now open."""

    global _host, _on_closed

    from .panel import CooldownBoard, create_cooldown_window
    from .timer import CooldownTimerStore, SystemClock

    with _lock:
        _on_closed = on_closed
        if _host is not None and _host.is_running:
            return bool(_host.show())

        def build() -> object:
            board = CooldownBoard(
                CooldownTimerStore(SystemClock(), None),
                logger=logger.getChild("board"),
            )
            window = create_cooldown_window(board, logger.getChild("window"))
            _configure(window, opacity=opacity, scale=scale)
            return window

        _host = WindowHost(
            build,  # type: ignore[arg-type]
            title="Enemy cooldowns",
            thread_name="cooldown-panel",
            logger=logger.getChild("host"),
        )
        return bool(_host.show())


def close_panel(timeout: float = 5.0) -> bool:
    """Close the board if it is open."""

    global _host

    with _lock:
        host, _host = _host, None
        if host is None:
            return True
        return bool(host.stop(timeout))


def is_open() -> bool:
    with _lock:
        return _host is not None and _host.is_running


def apply_display(opacity: float, scale: float) -> bool:
    """Apply display settings to a live panel; a no-op when it is closed.

    Settings are chosen from the tray rather than from the board itself: the
    board is deliberately small and low-profile, and permanent controls would
    cost screen space during a match.
    """

    with _lock:
        host = _host
    if host is None or not host.is_running:
        return False
    window = host.window
    if window is None:
        return False
    return _configure(window, opacity=opacity, scale=scale)


def _configure(window: object, *, opacity: float, scale: float) -> bool:
    """Push opacity and scale into a window object, tolerating an older panel."""

    applied = False
    setter = getattr(window, "set_display", None)
    if callable(setter):
        try:
            setter(opacity=opacity, scale=scale)
            applied = True
        except Exception:  # noqa: BLE001 - display settings are cosmetic
            LOGGER.debug("Could not apply cooldown display settings", exc_info=True)
    return applied


def _notify_closed() -> None:
    """Called by the host when the window goes away."""

    with _lock:
        callback = _on_closed
    if callback is not None:
        try:
            callback()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Cooldown close observer failed", exc_info=True)


__all__ = ["apply_display", "close_panel", "is_open", "open_panel"]
