"""Background orchestration for the application-managed LTK companion.

The skin-sync controller owns CSLOL and League monitoring.  This module owns LTK
preparation, LTK launches, and the reconcile that brings LTK's skin library back
to the application-owned baseline, so none of those slow operations run on a tray
callback thread.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from queue import Empty, Queue
from threading import Event, RLock, Thread

from .ltk_cleanup import LtkSkinCleanupError, LtkSkinCleanupResult, LtkSkinCleanupService
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
    LtkMigrationService,
    LtkReconcileError,
    ReconcileBlockedError,
    ReconcileProgress,
    ReconcileResult,
)
from .operation_gate import OperationGate

NotifySink = Callable[[str, str], None]
StatusSink = Callable[[str, bool], None]
LibraryStateChangedSink = Callable[[], None]
RunningPredicate = Callable[[], bool]
InstalledPredicate = Callable[[], bool]
ResumeCallback = Callable[[], object]

_LTK_TITLE = "LTK Manager"
_REBUILD_TITLE = "LTK skin library"
_CLEANUP_TITLE = "Remove all skins from LTK"


class _TaskKind(Enum):
    PREPARE = auto()
    START = auto()
    RECONCILE = auto()
    CLEAN_SKINS = auto()
    STOP = auto()


@dataclass(frozen=True, slots=True)
class _Task:
    kind: _TaskKind
    automatic: bool = False


class LtkTaskCoordinator:
    """Serialize LTK work on one cooperative, non-daemon worker."""

    def __init__(
        self,
        *,
        companion: LtkCompanion,
        reconciler: LtkMigrationService,
        cleanup: LtkSkinCleanupService,
        operation_gate: OperationGate,
        ltk_is_running: RunningPredicate,
        resume_cslol_launches: ResumeCallback,
        ltk_is_installed: InstalledPredicate | None = None,
        notify_sink: NotifySink | None = None,
        status_sink: StatusSink | None = None,
        library_state_changed_sink: LibraryStateChangedSink | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._companion = companion
        self._reconciler = reconciler
        self._cleanup = cleanup
        self._operation_gate = operation_gate
        self._ltk_is_running = ltk_is_running
        self._ltk_is_installed = ltk_is_installed
        self._resume_cslol_launches = resume_cslol_launches
        self._notify_sink = notify_sink
        self._status_sink = status_sink
        self._library_state_changed_sink = library_state_changed_sink
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._queue: Queue[_Task] = Queue()
        self._stop_event = Event()
        self._reconcile_cancel = Event()
        self._worker: Thread | None = None
        self._started = False
        self._stopping = False
        self._closed = False
        self._active_kind: _TaskKind | None = None
        self._start_queued = False
        self._reconcile_queued = False
        self._cleanup_queued = False
        self._deferred_reconcile = False

    @property
    def reconcile_active(self) -> bool:
        with self._lock:
            return self._reconcile_queued or self._active_kind is _TaskKind.RECONCILE

    @property
    def cleanup_active(self) -> bool:
        with self._lock:
            return self._cleanup_queued or self._active_kind is _TaskKind.CLEAN_SKINS

    def start(self) -> bool:
        """Start the worker and check the official release in the background."""

        with self._lock:
            if self._started:
                return not self._stopping
            if self._stopping or self._closed:
                return False
            worker = Thread(target=self._run, name="ltk-companion-worker", daemon=False)
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
        """Queue an installed-manager launch or a verified installation."""

        if not self.start():
            return False
        with self._lock:
            if self._stopping:
                return False
            if self._cleanup_queued or self._active_kind is _TaskKind.CLEAN_SKINS:
                self._notify(_LTK_TITLE, "Wait for the skin removal to finish, then try again.")
                return False
            if self._start_queued or self._active_kind is _TaskKind.START:
                self._publish_status("launch already queued", False)
                return True
            self._start_queued = True
            self._queue.put(_Task(_TaskKind.START))
        self._publish_status("launch queued", False)
        return True

    def request_rebuild(self, *, automatic: bool = False) -> bool:
        """Queue one reconcile of LTK's library back to the baseline."""

        if not self.start():
            return False
        with self._lock:
            if self._stopping:
                return False
            if self._cleanup_queued or self._active_kind is _TaskKind.CLEAN_SKINS:
                if not automatic:
                    self._notify(
                        _REBUILD_TITLE,
                        "Wait for the skin removal to finish, then try again.",
                    )
                return False
            if self._reconcile_queued or self._active_kind is _TaskKind.RECONCILE:
                if not automatic:
                    self._publish_status("rebuild already in progress", True)
                return not automatic
            self._deferred_reconcile = False
            self._reconcile_cancel.clear()
            self._reconcile_queued = True
            self._queue.put(_Task(_TaskKind.RECONCILE, automatic=automatic))
        self._publish_status("rebuild queued", True)
        return True

    def retry_deferred_rebuild(self) -> bool:
        """Requeue a rebuild that was deferred while a manager was running."""

        with self._lock:
            if not self._deferred_reconcile:
                return False
        return self.request_rebuild(automatic=True)

    def cancel_rebuild(self) -> bool:
        """Request cancellation at the next safe package boundary."""

        if not self.reconcile_active:
            return False
        self._reconcile_cancel.set()
        self._publish_status("cancelling rebuild safely", True)
        return True

    def request_cleanup(self) -> bool:
        """Queue one already-confirmed removal of every skin from LTK."""

        if not self.start():
            return False
        with self._lock:
            if self._stopping:
                return False
            if self._reconcile_queued or self._active_kind is _TaskKind.RECONCILE:
                self._notify(
                    _CLEANUP_TITLE,
                    "Wait for the library rebuild to finish or cancel it first.",
                )
                return False
            if self._start_queued or self._active_kind is _TaskKind.START:
                self._notify(
                    _CLEANUP_TITLE,
                    "Wait for the queued LTK launch, then close LTK and try again.",
                )
                return False
            if self._cleanup_queued or self._active_kind is _TaskKind.CLEAN_SKINS:
                self._publish_status("skin removal already queued", False)
                return True
            self._cleanup_queued = True
            self._queue.put(_Task(_TaskKind.CLEAN_SKINS))
        self._publish_status("skin removal queued", False)
        return True

    def shutdown(self, timeout_seconds: float) -> bool:
        """Cancel and join the worker within one bounded wait."""

        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        with self._lock:
            self._stopping = True
            self._stop_event.set()
            self._reconcile_cancel.set()
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

    # ------------------------------------------------------------------ worker

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
                    self._begin(task)
                    if self._stop_event.is_set():
                        continue
                    self._dispatch(task)
                except Exception as exc:
                    self._mark_finished(task.kind)
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
                self._reconcile_queued = False
                self._cleanup_queued = False

    def _begin(self, task: _Task) -> None:
        with self._lock:
            self._active_kind = task.kind
            if task.kind is _TaskKind.START:
                self._start_queued = False
            elif task.kind is _TaskKind.RECONCILE:
                self._reconcile_queued = False
            elif task.kind is _TaskKind.CLEAN_SKINS:
                self._cleanup_queued = False

    def _dispatch(self, task: _Task) -> None:
        if task.kind is _TaskKind.PREPARE:
            self._prepare()
        elif task.kind is _TaskKind.START:
            self._start_ltk()
        elif task.kind is _TaskKind.RECONCILE:
            self._reconcile(automatic=task.automatic)
        elif task.kind is _TaskKind.CLEAN_SKINS:
            self._remove_all_skins()

    def _mark_finished(self, kind: _TaskKind) -> None:
        with self._lock:
            if self._active_kind is kind:
                self._active_kind = None

    # ----------------------------------------------------------------- prepare

    def _prepare(self) -> None:
        try:
            result = self._companion.prepare(self._stop_event)
        except LtkCancelled:
            return
        except LtkCompanionError as exc:
            self._logger.warning("LTK release check was unavailable: %s", exc)
            self._publish_status("update check unavailable; existing LTK remains usable", False)
            return
        self._publish_preparation(result)

    def _publish_preparation(self, result: LtkPreparationResult) -> None:
        version = result.release.version
        if result.status is LtkPreparationStatus.CURRENT_INSTALLED:
            self._publish_status(f"v{version} is ready", False)
        else:
            self._publish_status(f"verified v{version} installer is cached", False)

    def _start_ltk(self) -> None:
        if self._safe_ltk_is_running():
            self._publish_status("already running", False)
            self._notify(_LTK_TITLE, "LTK Manager is already running.")
            return
        self._publish_status("verifying the official release", False)
        try:
            result = self._companion.start(self._stop_event)
        except LtkCancelled:
            return
        except LtkCompanionError as exc:
            self._logger.exception("Could not launch or install LTK Manager")
            self._publish_status(f"could not open: {exc}", False)
            self._notify(_LTK_TITLE, f"Could not open or install LTK Manager: {exc}")
            return
        self._publish_launch(result)

    def _publish_launch(self, result: LtkCompanionResult) -> None:
        if result.status is LtkCompanionStatus.INSTALLER_STARTED:
            detail = f"verified v{result.version} installer started"
            message = (
                f"The verified LTK Manager v{result.version} installer was started. "
                "LTK restarts once the per-user installation finishes."
            )
        elif result.status is LtkCompanionStatus.EXISTING_LAUNCHED_AFTER_RELEASE_CHECK_FAILURE:
            detail = f"opened v{result.version}; update check unavailable"
            message = (
                f"Opened LTK Manager v{result.version}. The latest-release check was "
                "unavailable, so no update was applied."
            )
        else:
            detail = f"opened v{result.version}"
            message = f"Opened LTK Manager v{result.version}."
        self._publish_status(detail, False)
        self._logger.info("LTK Manager launch: %s", detail)
        self._notify(_LTK_TITLE, message)

    # --------------------------------------------------------------- reconcile

    def _reconcile(self, *, automatic: bool) -> None:
        if automatic and not self._safe_ltk_is_installed():
            self._mark_finished(_TaskKind.RECONCILE)
            self._publish_status("rebuild skipped: LTK is not installed", False)
            return
        self._publish_status("waiting for skin synchronization to finish", True)
        lease = self._operation_gate.acquire("LTK library rebuild", self._reconcile_cancel)
        if lease is None:
            self._mark_finished(_TaskKind.RECONCILE)
            self._publish_status("rebuild cancelled before it started", False)
            return
        try:
            if self._stop_event.is_set() or self._reconcile_cancel.is_set():
                self._mark_finished(_TaskKind.RECONCILE)
                self._publish_status("rebuild cancelled before it started", False)
                return
            self._publish_status("comparing LTK with the current skin set", True)
            try:
                result = self._reconciler.reconcile(
                    cancel_event=self._reconcile_cancel,
                    progress=self._reconcile_progress,
                )
            except LtkReconcileError as exc:
                self._mark_finished(_TaskKind.RECONCILE)
                self._handle_reconcile_error(exc, automatic=automatic)
                return
            try:
                self._finish_reconcile(result, automatic=automatic)
            finally:
                self._publish_library_state_changed()
        finally:
            lease.release()
            try:
                self._resume_cslol_launches()
            except Exception:
                self._logger.exception("Could not resume queued CSLOL Manager launches")

    def _handle_reconcile_error(self, exc: LtkReconcileError, *, automatic: bool) -> None:
        if isinstance(exc, ReconcileBlockedError):
            with self._lock:
                self._deferred_reconcile = True
            self._logger.info("LTK library rebuild deferred: %s", exc)
            self._publish_status(f"rebuild deferred: {exc}", False)
            if not automatic:
                self._notify(_REBUILD_TITLE, str(exc))
            return
        self._logger.warning("LTK library rebuild failed: %s", exc)
        self._publish_status(f"rebuild failed: {exc}", False)
        if not automatic:
            self._notify(_REBUILD_TITLE, str(exc))

    def _finish_reconcile(self, result: ReconcileResult, *, automatic: bool) -> None:
        summary = self._reconcile_summary(result)
        self._mark_finished(_TaskKind.RECONCILE)
        if result.cancelled:
            self._publish_status(f"rebuild cancelled ({summary})", False)
            return
        if result.blocked:
            with self._lock:
                self._deferred_reconcile = True
            reason = result.issues[-1].reason if result.issues else "a manager started"
            self._publish_status(f"rebuild deferred: {reason}", False)
            return
        with self._lock:
            self._deferred_reconcile = False
        report_note = "" if result.report_error is None else "; report unavailable"
        self._publish_status(
            f"library matches the current skin set ({summary}){report_note}", False
        )
        if result.changed or not automatic:
            self._notify(
                "LTK skin library updated",
                f"{summary}. LTK imports queued skins the next time it starts, and no skins "
                "are switched on until you enable them in LTK.",
            )

    @staticmethod
    def _reconcile_summary(result: ReconcileResult) -> str:
        parts = [f"{result.expected} skins expected"]
        if result.added:
            parts.append(f"{result.added} queued")
        if result.removed:
            parts.append(f"{result.removed} removed")
        if not result.added and not result.removed:
            parts.append("already current")
        if result.issues:
            parts.append(f"{len(result.issues)} issue(s)")
        return ", ".join(parts)

    def _reconcile_progress(self, progress: ReconcileProgress) -> None:
        name = f" - {progress.skin_name}" if progress.skin_name else ""
        if progress.total:
            detail = f"{progress.phase} {progress.completed}/{progress.total}{name}"
        else:
            detail = f"{progress.phase}{name}"
        self._publish_status(detail, True)

    # ----------------------------------------------------------------- cleanup

    def _remove_all_skins(self) -> None:
        self._publish_status("waiting for skin synchronization to finish", False)
        lease = self._operation_gate.acquire("LTK skin removal", self._stop_event)
        if lease is None:
            self._mark_finished(_TaskKind.CLEAN_SKINS)
            self._publish_status("skin removal cancelled before it started", False)
            return
        try:
            if self._stop_event.is_set():
                self._mark_finished(_TaskKind.CLEAN_SKINS)
                self._publish_status("skin removal cancelled before it started", False)
                return
            self._publish_status("removing every skin from LTK", False)
            try:
                result = self._cleanup.remove_all()
            except LtkSkinCleanupError as exc:
                self._logger.warning("LTK skin removal could not finish: %s", exc)
                self._mark_finished(_TaskKind.CLEAN_SKINS)
                self._publish_status(f"skin removal not completed: {exc}", False)
                self._notify(_CLEANUP_TITLE, str(exc))
                return
            self._finish_cleanup(result)
        finally:
            self._publish_library_state_changed()
            lease.release()
            try:
                self._resume_cslol_launches()
            except Exception:
                self._logger.exception("Could not resume queued CSLOL Manager launches")

    def _finish_cleanup(self, result: LtkSkinCleanupResult) -> None:
        self._mark_finished(_TaskKind.CLEAN_SKINS)
        count = max(result.library_mods, result.archives)
        self._publish_status(f"removed every skin ({count} package(s))", False)
        self._notify(
            "All skins removed from LTK",
            f"Removed {count} skin package(s). LTK itself, its settings and logs were kept. "
            "The next rebuild will restore the current skin set.",
        )

    # ------------------------------------------------------------------ helpers

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

    def _safe_ltk_is_installed(self) -> bool:
        if self._ltk_is_installed is None:
            return False
        try:
            installed = self._ltk_is_installed()
        except Exception:
            self._logger.exception("Could not inspect the LTK installation state")
            return False
        return isinstance(installed, bool) and installed

    def _publish_status(self, detail: str, rebuild_active: bool) -> None:
        if self._status_sink is None:
            return
        if not rebuild_active and self.reconcile_active:
            rebuild_active = True
        try:
            self._status_sink(detail, rebuild_active)
        except Exception:
            self._logger.exception("LTK status sink failed")

    def _notify(self, title: str, message: str) -> None:
        if self._notify_sink is None:
            return
        try:
            self._notify_sink(title, message)
        except Exception:
            self._logger.exception("LTK notification sink failed")

    def _publish_library_state_changed(self) -> None:
        if self._library_state_changed_sink is None:
            return
        try:
            self._library_state_changed_sink()
        except Exception:
            self._logger.exception("LTK library-state change sink failed")

    def _close_companion(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            self._companion.close()
        except Exception:
            self._logger.exception("Could not close the LTK companion")


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
    "InstalledPredicate",
    "LibraryStateChangedSink",
    "LtkTaskCoordinator",
    "NotifySink",
    "ResumeCallback",
    "RunningPredicate",
    "StatusSink",
    "wait_for_ltk_tasks",
]
