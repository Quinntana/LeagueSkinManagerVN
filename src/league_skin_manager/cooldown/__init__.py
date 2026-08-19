"""The enemy cooldown board.

This package is the application's one enforced internal boundary.  Nothing
outside it may import its internals: the shell sees the four functions below
and nothing else.  That isolation is what lets the board depend on the Live
Client API and Data Dragon without any of that risk reaching the skin
pipeline -- if this package fails entirely, skins are unaffected, and the
reverse holds too.

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
    on_closed: Callable[[], None] | None = None,
    logger: logging.Logger = LOGGER,
) -> bool:
    """Show the cooldown board. Returns whether it is now open."""

    global _host, _window

    from .board import CooldownBoard
    from .catalog import CooldownCatalog
    from .live import LiveClient
    from .panel import create_cooldown_window
    from .timer import CooldownTimerStore, SystemClock

    with _lock:
        if _host is not None and _host.is_running:
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
                on_closed=on_closed,
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
    """Close the board if it is open."""

    global _host, _window

    with _lock:
        host, _host = _host, None
        _window = None
        if host is None:
            return True
        return bool(host.stop(timeout))


def is_open() -> bool:
    with _lock:
        return _host is not None and _host.is_running


def apply_display(opacity: float, scale: float) -> bool:
    """Apply tray-chosen display settings to a live panel; a no-op when closed.

    Settings live in the tray rather than on the board because the board is
    deliberately small and sits over a running game, where permanent controls
    would cost screen space.
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


__all__ = ["apply_display", "close_panel", "is_open", "open_panel"]
