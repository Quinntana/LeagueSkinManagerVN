"""Watch for the League game process.

The only process this application cares about is the game itself, and only so
the cooldown board can open and close with a match.  The previous design also
watched ``LeagueClient.exe`` in order to launch a skin manager when the client
appeared; LTK starts its own patcher, so that no longer has a purpose.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event, Thread

from .config import LEAGUE_GAME_PROCESS_NAME
from .windows import ProcessLookup

LOGGER = logging.getLogger(__name__)

POLL_SECONDS = 5.0


class GameWatcher:
    """Calls back when the game process starts and stops.

    One daemon thread and one boolean.  Callback failures are logged and
    swallowed: a broken observer must not stop the watch.
    """

    def __init__(
        self,
        on_change: Callable[[bool], None],
        *,
        process_name: str = LEAGUE_GAME_PROCESS_NAME,
        poll_seconds: float = POLL_SECONDS,
        lookup: type[ProcessLookup] | ProcessLookup = ProcessLookup,
        logger: logging.Logger = LOGGER,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._on_change = on_change
        self._process_name = process_name
        self._poll_seconds = poll_seconds
        self._lookup = lookup
        self._logger = logger
        self._stop = Event()
        self._thread: Thread | None = None
        self._running = False

    @property
    def match_active(self) -> bool:
        return self._running

    def start(self) -> bool:
        if self._thread is not None:
            return False
        self._stop.clear()
        self._thread = Thread(target=self._run, name="league-game-watch", daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def poll_once(self) -> bool:
        """Check the process table once; returns whether the game is running."""

        try:
            running = bool(self._lookup.is_running(self._process_name))
        except Exception:  # noqa: BLE001 - polling must survive anything
            self._logger.warning("Game process polling failed", exc_info=True)
            return self._running
        if running != self._running:
            self._running = running
            self._notify(running)
        return running

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._poll_seconds)

    def _notify(self, running: bool) -> None:
        try:
            self._on_change(running)
        except Exception:  # noqa: BLE001 - an observer must not stop the watch
            self._logger.exception("Game state observer failed")


__all__ = ["POLL_SECONDS", "GameWatcher"]
