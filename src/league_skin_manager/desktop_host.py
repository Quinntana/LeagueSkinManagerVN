"""Lazy, failure-isolated host for the optional desktop presentation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import RLock, Thread, current_thread
from typing import Protocol

from .controller import AppState


class DesktopBoundary(Protocol):
    """UI adapter used by the composition root without importing Tk."""

    def run(self, *, show_on_start: bool = True) -> None: ...

    def show(self) -> None: ...

    def stop(self) -> None: ...

    def request_ltk_migration(self) -> None: ...

    def update_status(self, state: AppState, detail: str) -> None: ...

    def update_ltk_status(self, detail: str, *, migration_active: bool = False) -> None: ...


DesktopFactory = Callable[[], DesktopBoundary]
FailureSink = Callable[[str, str], None]


class DesktopHost:
    """Create and run the optional desktop only after an explicit request."""

    def __init__(
        self,
        factory: DesktopFactory,
        *,
        failure_sink: FailureSink | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._factory = factory
        self._failure_sink = failure_sink
        self._logger = logger or logging.getLogger(__name__)
        self._lock = RLock()
        self._desktop: DesktopBoundary | None = None
        self._thread: Thread | None = None
        self._stopping = False
        self._status = (AppState.STARTING, "Starting")
        self._ltk_status = ("checking the latest official release", False)

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def show(self) -> bool:
        desktop, started = self._ensure_started(show_on_start=True)
        if desktop is None:
            return False
        if not started:
            desktop.show()
        return True

    def request_ltk_migration(self) -> bool:
        desktop, _started = self._ensure_started(show_on_start=False)
        if desktop is None:
            return False
        desktop.request_ltk_migration()
        return True

    def update_status(self, state: AppState, detail: str) -> None:
        with self._lock:
            self._status = (state, detail)
            desktop = self._active_desktop_locked()
        if desktop is not None:
            desktop.update_status(state, detail)

    def update_ltk_status(self, detail: str, *, migration_active: bool = False) -> None:
        with self._lock:
            self._ltk_status = (detail, migration_active)
            desktop = self._active_desktop_locked()
        if desktop is not None:
            desktop.update_ltk_status(detail, migration_active=migration_active)

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        with self._lock:
            self._stopping = True
            desktop = self._desktop
            thread = self._thread
        if desktop is not None and thread is not None and thread.is_alive():
            desktop.stop()
        if thread is not None and thread is not current_thread() and thread.is_alive():
            thread.join(timeout_seconds)
        return thread is None or not thread.is_alive()

    def _ensure_started(
        self,
        *,
        show_on_start: bool,
    ) -> tuple[DesktopBoundary | None, bool]:
        with self._lock:
            if self._stopping:
                return None, False
            desktop = self._active_desktop_locked()
            if desktop is not None:
                return desktop, False
            try:
                desktop = self._factory()
                state, detail = self._status
                ltk_detail, migration_active = self._ltk_status
                desktop.update_status(state, detail)
                desktop.update_ltk_status(
                    ltk_detail,
                    migration_active=migration_active,
                )
                thread = Thread(
                    target=self._run,
                    args=(desktop, show_on_start),
                    name="optional-desktop-ui",
                    daemon=False,
                )
                self._desktop = desktop
                self._thread = thread
                thread.start()
            except Exception as exc:
                self._desktop = None
                self._thread = None
                if desktop is not None:
                    try:
                        desktop.stop()
                    except Exception:
                        self._logger.exception(
                            "Could not release a partially initialized optional desktop"
                        )
                self._publish_failure(f"Could not open the optional skin library: {exc}")
                return None, False
            return desktop, True

    def _active_desktop_locked(self) -> DesktopBoundary | None:
        if self._thread is None or not self._thread.is_alive():
            return None
        return self._desktop

    def _run(self, desktop: DesktopBoundary, show_on_start: bool) -> None:
        try:
            desktop.run(show_on_start=show_on_start)
        except Exception as exc:
            self._logger.exception("Optional desktop presentation failed")
            self._publish_failure(f"The optional skin library closed unexpectedly: {exc}")

    def _publish_failure(self, message: str) -> None:
        self._logger.error(message)
        if self._failure_sink is None:
            return
        try:
            self._failure_sink("LeagueSkinManagerVN window", message)
        except Exception:
            self._logger.exception("Could not report an optional desktop failure")


__all__ = ["DesktopBoundary", "DesktopFactory", "DesktopHost", "FailureSink"]
