"""Background orchestration for the optional official LTK companion.

The skin-sync controller keeps ownership of CSLOL and League monitoring.  This
module owns only LTK preparation, explicit LTK launches, and the one-way
CSLOL-to-LTK migration worker so none of those slow operations run on a tray or
Tk callback thread.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from queue import Empty, Queue
from threading import Event, RLock, Thread

from .ltk_companion import (
    LtkCancelled,
    LtkCompanion,
    LtkCompanionError,
    LtkCompanionResult,
    LtkCompanionStatus,
    LtkPreparationResult,
    LtkPreparationStatus,
)
from .ltk_migration import (
    LtkMigrationError,
    LtkMigrationService,
    MigrationProgress,
    MigrationResult,
)
from .operation_gate import OperationGate

NotifySink = Callable[[str, str], None]
StatusSink = Callable[[str, bool], None]
RunningPredicate = Callable[[], bool]
ResumeCallback = Callable[[], object]


class _TaskKind(Enum):
    PREPARE = auto()
    START = auto()
    MIGRATE = auto()
    RESET_HISTORY = auto()
    STOP = auto()


@dataclass(frozen=True, slots=True)
class _Task:
    kind: _TaskKind
    source: Path | None = None


class LtkTaskCoordinator:
    """Serialize LTK work on one cooperative, non-daemon worker."""

    def __init__(
        self,
        *,
        companion: LtkCompanion,
        migration: LtkMigrationService,
        operation_gate: OperationGate,
        ltk_is_running: RunningPredicate,
        resume_cslol_launches: ResumeCallback,
        notify_sink: NotifySink | None = None,
        status_sink: StatusSink | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._companion = companion
        self._migration = migration
        self._operation_gate = operation_gate
        self._ltk_is_running = ltk_is_running
        self._resume_cslol_launches = resume_cslol_launches
        self._notify_sink = notify_sink
        self._status_sink = status_sink
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._queue: Queue[_Task] = Queue()
        self._stop_event = Event()
        self._migration_cancel = Event()
        self._worker: Thread | None = None
        self._started = False
        self._stopping = False
        self._closed = False
        self._active_kind: _TaskKind | None = None
        self._start_queued = False
        self._migration_queued = False
        self._reset_queued = False

    @property
    def migration_active(self) -> bool:
        with self._lock:
            return self._migration_queued or self._active_kind is _TaskKind.MIGRATE

    def start(self) -> bool:
        """Start the worker and automatically prepare the latest official installer."""

        with self._lock:
            if self._started:
                return not self._stopping
            if self._stopping or self._closed:
                return False
            worker = Thread(
                target=self._run,
                name="ltk-companion-worker",
                daemon=False,
            )
            self._worker = worker
            self._started = True
            try:
                worker.start()
            except Exception:
                self._worker = None
                self._started = False
                self._logger.exception("Could not start LTK companion worker")
                return False
            self._queue.put(_Task(_TaskKind.PREPARE))
        self._publish_status("checking the latest official release", False)
        return True

    def request_start(self) -> bool:
        """Queue an explicit installed-manager launch or verified installation."""

        if not self.start():
            return False
        with self._lock:
            if self._stopping:
                return False
            if self._start_queued or self._active_kind is _TaskKind.START:
                self._publish_status("LTK launch is already queued", False)
                return True
            self._start_queued = True
            self._queue.put(_Task(_TaskKind.START))
        self._publish_status("LTK launch queued", False)
        return True

    def request_migration(self, source: Path) -> bool:
        """Queue one user-confirmed port; this is never called by automatic startup work."""

        if not self.start():
            return False
        selected = Path(source)
        with self._lock:
            if self._stopping:
                return False
            if self._migration_queued or self._active_kind is _TaskKind.MIGRATE:
                self._notify(
                    "LTK migration",
                    "A CSLOL-to-LTK migration is already queued or running.",
                )
                return False
            self._migration_cancel.clear()
            self._migration_queued = True
            self._queue.put(_Task(_TaskKind.MIGRATE, selected))
        self._publish_status("explicit CSLOL-to-LTK port queued", True)
        return True

    def cancel_migration(self) -> bool:
        """Request cancellation at the next safe package/file boundary."""

        with self._lock:
            active = self._migration_queued or self._active_kind is _TaskKind.MIGRATE
        if not active:
            return False
        self._migration_cancel.set()
        self._publish_status("cancelling migration safely", True)
        return True

    def request_history_reset(self) -> bool:
        """Queue an explicit reset so previously imported packages may be requeued."""

        if not self.start():
            return False
        with self._lock:
            if self._stopping:
                return False
            if self._migration_queued or self._active_kind is _TaskKind.MIGRATE:
                self._notify(
                    "LTK migration history",
                    "Wait for the active migration to finish before resetting its history.",
                )
                return False
            if self._reset_queued or self._active_kind is _TaskKind.RESET_HISTORY:
                return True
            self._reset_queued = True
            self._queue.put(_Task(_TaskKind.RESET_HISTORY))
        self._publish_status("migration-history reset queued", False)
        return True

    def shutdown(self, timeout_seconds: float) -> bool:
        """Cancel and join the worker within one bounded wait."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        with self._lock:
            self._stopping = True
            self._stop_event.set()
            self._migration_cancel.set()
            worker = self._worker
            if worker is not None and worker.is_alive():
                self._queue.put(_Task(_TaskKind.STOP))

        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join(timeout_seconds)
        alive = worker is not None and worker.is_alive()
        if alive:
            self._logger.warning("LTK companion worker is still stopping")
            return False
        self._close_companion()
        return True

    def _run(self) -> None:
        try:
            while True:
                try:
                    task = self._queue.get(timeout=0.25)
                except Empty:
                    if self._stop_event.is_set():
                        break
                    continue
                try:
                    if task.kind is _TaskKind.STOP:
                        break
                    with self._lock:
                        self._active_kind = task.kind
                        if task.kind is _TaskKind.START:
                            self._start_queued = False
                        elif task.kind is _TaskKind.MIGRATE:
                            self._migration_queued = False
                        elif task.kind is _TaskKind.RESET_HISTORY:
                            self._reset_queued = False
                    if self._stop_event.is_set():
                        continue
                    if task.kind is _TaskKind.PREPARE:
                        self._prepare()
                    elif task.kind is _TaskKind.START:
                        self._start_ltk()
                    elif task.kind is _TaskKind.MIGRATE:
                        if task.source is None:
                            raise RuntimeError("LTK migration task has no source")
                        self._migrate(task.source)
                    elif task.kind is _TaskKind.RESET_HISTORY:
                        self._reset_history()
                except Exception as exc:
                    if task.kind is _TaskKind.MIGRATE:
                        self._mark_migration_finished()
                    self._logger.exception("Unexpected LTK companion worker failure")
                    self._publish_status(f"operation failed: {exc}", False)
                    self._notify("LTK companion", f"Operation failed: {exc}")
                finally:
                    with self._lock:
                        self._active_kind = None
                    self._queue.task_done()
        finally:
            with self._lock:
                self._active_kind = None
                self._start_queued = False
                self._migration_queued = False
                self._reset_queued = False

    def _prepare(self) -> None:
        try:
            result = self._companion.prepare(self._stop_event)
        except LtkCancelled:
            return
        except LtkCompanionError as exc:
            self._logger.warning("Automatic LTK preparation was unavailable: %s", exc)
            self._publish_status("update check unavailable; existing LTK remains usable", False)
            return
        self._publish_preparation(result)

    def _publish_preparation(self, result: LtkPreparationResult) -> None:
        version = result.release.version
        if result.status is LtkPreparationStatus.CURRENT_INSTALLED:
            self._publish_status(f"LTK Manager v{version} is ready", False)
        else:
            self._publish_status(f"verified LTK v{version} installer is cached", False)

    def _start_ltk(self) -> None:
        if self._safe_ltk_is_running():
            self._publish_status("LTK Manager or the legacy LTK app is already running", False)
            self._notify(
                "LTK Manager",
                "LTK Manager (or the legacy LTK app) is already running. Close it before "
                "switching implementations.",
            )
            return
        self._publish_status("verifying the official LTK release", False)
        try:
            result = self._companion.start(self._stop_event)
        except LtkCancelled:
            return
        except LtkCompanionError as exc:
            self._logger.exception("Could not launch or install LTK Manager")
            self._publish_status(f"could not open LTK Manager: {exc}", False)
            self._notify("LTK Manager", f"Could not open or install LTK Manager: {exc}")
            return
        self._publish_launch(result)

    def _publish_launch(self, result: LtkCompanionResult) -> None:
        if result.status is LtkCompanionStatus.INSTALLER_STARTED:
            detail = f"verified LTK v{result.version} installer started"
            message = (
                f"The verified LTK Manager v{result.version} installer was started. "
                "It will restart LTK after the per-user installation finishes."
            )
        elif result.status is LtkCompanionStatus.EXISTING_LAUNCHED_AFTER_RELEASE_CHECK_FAILURE:
            detail = f"opened installed LTK v{result.version}; update check unavailable"
            message = (
                f"Opened installed LTK Manager v{result.version}. The latest-release check "
                "was unavailable, so no update was applied."
            )
        else:
            detail = f"opened LTK Manager v{result.version}"
            message = f"Opened LTK Manager v{result.version}."
        self._publish_status(detail, False)
        self._notify("LTK Manager", message)

    def _migrate(self, source: Path) -> None:
        self._publish_status("waiting for skin synchronization to finish", True)
        lease = self._operation_gate.acquire(
            "LTK skin migration",
            self._migration_cancel,
        )
        if lease is None:
            self._mark_migration_finished()
            self._publish_status("migration cancelled before it started", False)
            return
        try:
            if self._stop_event.is_set() or self._migration_cancel.is_set():
                self._mark_migration_finished()
                self._publish_status("migration cancelled before it started", False)
                return
            self._publish_status("validating CSLOL mods", True)
            try:
                result = self._migration.migrate(
                    source,
                    cancel_event=self._migration_cancel,
                    progress=self._migration_progress,
                )
            except LtkMigrationError as exc:
                self._logger.warning("LTK migration could not start: %s", exc)
                self._mark_migration_finished()
                self._publish_status(f"migration not started: {exc}", False)
                self._notify("LTK migration", str(exc))
                return
            self._finish_migration(result)
        finally:
            lease.release()
            try:
                self._resume_cslol_launches()
            except Exception:
                self._logger.exception("Could not resume queued CSLOL Manager launches")

    def _finish_migration(self, result: MigrationResult) -> None:
        summary = f"{result.queued} queued, {result.skipped} already queued, {result.failed} failed"
        if result.cancelled:
            self._mark_migration_finished()
            self._publish_status(f"migration cancelled ({summary})", False)
            self._notify(
                "LTK migration cancelled",
                f"Partial result: {summary}. Report: {result.report_path}",
            )
            return
        if result.blocked:
            reason = result.issues[-1].reason if result.issues else "a manager started"
            self._mark_migration_finished()
            self._publish_status(f"migration stopped safely: {reason}", False)
            self._notify(
                "LTK migration stopped",
                f"{reason}. Partial result: {summary}. Report: {result.report_path}",
            )
            return

        if result.queued > 0 and not self._safe_ltk_is_running():
            self._publish_status(f"migration complete ({summary}); opening LTK", True)
            try:
                launch = self._companion.start(self._migration_cancel)
            except LtkCancelled:
                self._mark_migration_finished()
                self._publish_status(f"migration complete ({summary}); LTK launch cancelled", False)
            except LtkCompanionError as exc:
                self._logger.exception("Migration succeeded but LTK could not be opened")
                self._mark_migration_finished()
                self._publish_status(f"migration complete ({summary}); LTK launch failed", False)
                self._notify(
                    "LTK migration complete",
                    f"{summary}. LTK could not be opened: {exc}. Report: {result.report_path}",
                )
                return
            else:
                self._publish_launch(launch)

        self._mark_migration_finished()
        self._publish_status(f"migration complete: {summary}", False)
        self._notify(
            "LTK migration complete",
            f"{summary}. CSLOL originals were unchanged. Report: {result.report_path}",
        )

    def _migration_progress(self, progress: MigrationProgress) -> None:
        name = f" - {progress.mod_name}" if progress.mod_name else ""
        if progress.total:
            detail = f"{progress.phase} {progress.completed}/{progress.total}{name}"
        else:
            detail = f"{progress.phase}{name}"
        self._publish_status(detail, True)

    def _reset_history(self) -> None:
        try:
            self._migration.forget_history()
        except LtkMigrationError as exc:
            self._logger.warning("Could not reset LTK migration history: %s", exc)
            self._publish_status(f"could not reset migration history: {exc}", False)
            self._notify("LTK migration history", f"Could not reset history: {exc}")
            return
        self._publish_status("migration history reset; packages may be requeued", False)
        self._notify(
            "LTK migration history",
            "Migration history was reset. The next migration may queue packages already in LTK.",
        )

    def _safe_ltk_is_running(self) -> bool:
        try:
            running = self._ltk_is_running()
        except Exception:
            self._logger.exception("Could not inspect LTK process state")
            return True
        if not isinstance(running, bool):
            self._logger.error("LTK process predicate returned a non-boolean value")
            return True
        return running

    def _mark_migration_finished(self) -> None:
        with self._lock:
            if self._active_kind is _TaskKind.MIGRATE:
                self._active_kind = None

    def _publish_status(self, detail: str, migration_active: bool) -> None:
        if self._status_sink is None:
            return
        if not migration_active and self.migration_active:
            migration_active = True
        try:
            self._status_sink(detail, migration_active)
        except Exception:
            self._logger.exception("LTK status sink failed")

    def _notify(self, title: str, message: str) -> None:
        if self._notify_sink is None:
            return
        try:
            self._notify_sink(title, message)
        except Exception:
            self._logger.exception("LTK notification sink failed")

    def _close_companion(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._companion.close()
        except Exception:
            self._logger.exception("Could not close LTK companion")


def wait_for_ltk_tasks(
    coordinator: LtkTaskCoordinator,
    timeout_seconds: float,
    logger: logging.Logger,
) -> None:
    """Retain process resources until the non-daemon LTK worker has stopped."""

    while not coordinator.shutdown(timeout_seconds):
        logger.warning("LTK background work is still stopping; retaining app resources")
        time.sleep(0)


__all__ = [
    "LtkTaskCoordinator",
    "NotifySink",
    "ResumeCallback",
    "RunningPredicate",
    "StatusSink",
    "wait_for_ltk_tasks",
]
