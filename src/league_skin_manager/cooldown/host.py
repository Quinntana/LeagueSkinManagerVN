"""Lazy, failure-isolated host for an optional on-demand window.

The tray owns the application lifetime.  Any window this application offers is
constructed only when the user asks for it, runs its own event loop on a
dedicated non-daemon thread, and cannot take the tray down with it if it fails.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import RLock, Thread, current_thread
from typing import Protocol


class WindowBoundary(Protocol):
    """Window adapter used by the composition root without importing Tk."""

    def run(self) -> None: ...

    def show(self) -> None: ...

    def stop(self) -> None: ...


WindowFactory = Callable[[], WindowBoundary]
FailureSink = Callable[[str, str], None]


class WindowHost:
    """Create and run an optional window only after an explicit request."""

    def __init__(
        self,
        factory: WindowFactory,
        *,
        title: str = "Window",
        thread_name: str = "optional-window-ui",
        failure_sink: FailureSink | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._factory = factory
        self._title = title
        self._thread_name = thread_name
        self._failure_sink = failure_sink
        self._logger = logger or logging.getLogger(__name__)
        self._lock = RLock()
        self._window: WindowBoundary | None = None
        self._thread: Thread | None = None
        self._stopping = False

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def window(self) -> WindowBoundary | None:
        """The live window object, or None before it has been created."""

        with self._lock:
            return self._window

    def show(self) -> bool:
        """Open the window, creating and starting it on first request."""

        window, started = self._ensure_started()
        if window is None:
            return False
        if not started:
            try:
                window.show()
            except Exception as exc:
                self._logger.exception("Could not raise the optional window")
                self._publish_failure(f"Could not open {self._title}: {exc}")
                return False
        return True

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            self._stopping = True
            window = self._window
            thread = self._thread
        if window is not None and thread is not None and thread.is_alive():
            try:
                window.stop()
            except Exception:
                self._logger.exception("Could not ask the optional window to close")
        if thread is not None and thread is not current_thread() and thread.is_alive():
            thread.join(timeout_seconds)
        return thread is None or not thread.is_alive()

    def _ensure_started(self) -> tuple[WindowBoundary | None, bool]:
        with self._lock:
            if self._stopping:
                return None, False
            window = self._active_window_locked()
            if window is not None:
                return window, False
            try:
                window = self._factory()
                thread = Thread(
                    target=self._run,
                    args=(window,),
                    name=self._thread_name,
                    daemon=False,
                )
                self._window = window
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._window = None
                self._thread = None
                if window is not None:
                    try:
                        window.stop()
                    except Exception:
                        self._logger.exception(
                            "Could not release a partially initialized optional window"
                        )
                self._publish_failure(f"Could not open {self._title}: {exc}")
                return None, False
            return window, True

    def _active_window_locked(self) -> WindowBoundary | None:
        if self._thread is None or not self._thread.is_alive():
            return None
        return self._window

    def _run(self, window: WindowBoundary) -> None:
        try:
            window.run()
        except Exception as exc:
            self._logger.exception("Optional window failed")
            self._publish_failure(f"{self._title} closed unexpectedly: {exc}")

    def _publish_failure(self, message: str) -> None:
        self._logger.error(message)
        if self._failure_sink is None:
            return
        try:
            self._failure_sink(self._title, message)
        except Exception:
            self._logger.exception("Could not report an optional window failure")


__all__ = ["FailureSink", "WindowBoundary", "WindowFactory", "WindowHost"]
