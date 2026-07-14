"""Thread-safe application lifecycle coordination.

The controller deliberately knows nothing about pystray, the registry, downloads, or
process creation.  Those boundaries are injected so lifecycle behavior remains
deterministic and testable.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from threading import Event, Lock, RLock, Thread
from typing import Protocol

from .operation_gate import OperationGate, OperationLease


class AppState(Enum):
    """Observable lifecycle states for the tray and diagnostics."""

    STARTING = auto()
    OFFLINE_READY = auto()
    SYNCING = auto()
    READY = auto()
    ERROR = auto()
    STOPPING = auto()


@dataclass(frozen=True, slots=True)
class SyncOutcome:
    """Successful sync completion, including an expected offline result."""

    state: AppState
    detail: str

    def __post_init__(self) -> None:
        if self.state not in (AppState.READY, AppState.OFFLINE_READY):
            raise ValueError("sync outcome must be READY or OFFLINE_READY")
        if not self.detail.strip():
            raise ValueError("sync outcome detail must not be empty")


class ProcessMonitor(Protocol):
    """Cooperative League process monitor boundary."""

    def run(
        self,
        stop_event: Event,
        changed: Callable[[int | None], None],
    ) -> None: ...


SyncCallable = Callable[[Event], SyncOutcome | None]
ManagerLauncher = Callable[[], bool | None]
StatusSink = Callable[[AppState, str], None]
NotifySink = Callable[[str, str], None]


_DEFAULT_STATUS: dict[AppState, str] = {
    AppState.STARTING: "Starting",
    AppState.OFFLINE_READY: "Ready offline",
    AppState.SYNCING: "Syncing skins",
    AppState.READY: "Ready",
    AppState.ERROR: "An error occurred",
    AppState.STOPPING: "Stopping",
}

_SYNC_GATE_OWNER = "skin synchronization"
_MANAGER_GATE_OWNER = "CSLOL Manager launch"


class AppController:
    """Own the app state machine and its two cooperative background activities."""

    def __init__(
        self,
        *,
        sync: SyncCallable,
        launcher: ManagerLauncher,
        monitor: ProcessMonitor,
        status_sink: StatusSink | None = None,
        notify_sink: NotifySink | None = None,
        operation_gate: OperationGate | None = None,
        sync_on_start: bool = True,
        shutdown_timeout_seconds: float = 10.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")

        self._sync = sync
        self._launcher = launcher
        self._monitor = monitor
        self._status_sink = status_sink
        self._notify_sink = notify_sink
        self._operation_gate = operation_gate
        self._sync_on_start = sync_on_start
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._manager_launch_lock = Lock()
        self._stop_event = Event()
        self._state = AppState.STARTING
        self._status_detail = _DEFAULT_STATUS[self._state]
        self._started = False
        self._sync_running = False
        self._startup_sync_pending = False
        self._sync_thread: Thread | None = None
        self._sync_threads: set[Thread] = set()
        self._monitor_thread: Thread | None = None
        self._current_league_pid: int | None = None
        self._pending_league_pid: int | None = None
        self._launched_for_league_pid: int | None = None
        self._manual_manager_launch_pending = False
        self._monitor_failure_detail: str | None = None

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def status_detail(self) -> str:
        with self._lock:
            return self._status_detail

    @property
    def sync_in_progress(self) -> bool:
        with self._lock:
            return self._sync_running

    @property
    def launched_for_league_pid(self) -> int | None:
        with self._lock:
            return self._launched_for_league_pid

    def start(self) -> bool:
        """Start monitoring and optionally queue the initial sync.

        The tray should invoke this from its setup callback, which pystray runs
        after the icon has become visible.  All potentially slow work is then
        performed on background threads.
        """

        with self._lock:
            if self._started or self._stop_event.is_set():
                return False
            self._started = True
            self._startup_sync_pending = self._sync_on_start
            self._state = AppState.OFFLINE_READY
            self._status_detail = _DEFAULT_STATUS[self._state]
            monitor_thread = Thread(
                target=self._run_monitor,
                name="league-process-monitor",
                # Never abandon monitor-owned cleanup during interpreter exit.
                # ``shutdown`` supplies the bounded cooperative join.
                daemon=False,
            )
            self._monitor_thread = monitor_thread

            state = self._state
            detail = self._status_detail

        # Publish before starting work so a very fast worker cannot report a
        # later state and then be overwritten by this initial transition.
        self._publish_status(state, detail)

        with self._lock:
            if self._stop_event.is_set():
                return False
            try:
                monitor_thread.start()
            except Exception as exc:
                self._startup_sync_pending = False
                self._state = AppState.ERROR
                self._status_detail = f"Process monitor failed: {exc}"
                self._logger.exception("Unable to start League process monitor")
                state = self._state
                detail = self._status_detail

        if state is AppState.ERROR:
            self._publish_status(state, detail)
            self._notify("LeagueSkinManagerVN", detail)
            return False
        if self._sync_on_start:
            self.request_sync()
        return True

    def request_sync(self) -> bool:
        """Start the sole sync worker, or reject a duplicate request."""

        rejection: str | None = None
        sync_thread: Thread | None = None
        sync_lease: OperationLease | None = None
        worker_creation_failed = False
        state = self.state
        detail = self.status_detail
        # Serialize the decision with manager launch. Whichever operation wins
        # this lock completes its transition before the other observes state.
        with self._manager_launch_lock:
            with self._lock:
                rejection = self._sync_rejection_locked()

            if rejection is None and self._operation_gate is not None:
                # This is deliberately non-blocking. External workers use the
                # cancellable wait API and call ``resume_pending_manager_launches``
                # after releasing their lease.
                sync_lease = self._operation_gate.try_acquire(_SYNC_GATE_OWNER)
                if sync_lease is None:
                    owner = self._operation_gate.current_owner
                    if owner is None:
                        owner = "another maintenance operation"
                    rejection = (
                        f"Cannot sync while {owner} is active or queued. "
                        "Wait for it to finish, then try Sync again."
                    )
                    with self._lock:
                        # A rejected startup sync must not leave every manager
                        # launch permanently queued behind work that never began.
                        self._startup_sync_pending = False

            with self._lock:
                if rejection is None:
                    # Shutdown can race the gate acquisition, so validate again
                    # before publishing or creating the worker.
                    rejection = self._sync_rejection_locked()
                if rejection is None:
                    self._startup_sync_pending = False
                    try:
                        sync_thread = Thread(
                            target=self._run_sync,
                            args=(sync_lease,),
                            name="skin-sync-worker",
                            # A daemon could be killed halfway through an atomic install.
                            daemon=False,
                        )
                    except Exception as exc:
                        worker_creation_failed = True
                        self._state = AppState.ERROR
                        self._status_detail = f"Unable to create sync worker: {exc}"
                        self._logger.exception("Unable to create sync worker")
                    else:
                        self._sync_running = True
                        self._state = AppState.SYNCING
                        self._status_detail = _DEFAULT_STATUS[self._state]
                        self._sync_thread = sync_thread
                        self._sync_threads.add(sync_thread)

                    state = self._state
                    detail = self._status_detail

        if rejection is not None:
            if sync_lease is not None:
                sync_lease.release()
            self._notify("Sync not started", rejection)
            return False

        if worker_creation_failed:
            if sync_lease is not None:
                sync_lease.release()
            self._publish_status(state, detail)
            self._notify("Skin sync failed", detail)
            return False

        if sync_thread is None:
            raise RuntimeError("sync worker was not created")
        self._publish_status(state, detail)

        release_lease = False
        with self._lock:
            if self._stop_event.is_set() or self._state is AppState.STOPPING:
                self._sync_running = False
                self._sync_threads.discard(sync_thread)
                release_lease = True
            else:
                try:
                    sync_thread.start()
                except Exception as exc:
                    self._sync_running = False
                    self._sync_threads.discard(sync_thread)
                    self._state = AppState.ERROR
                    self._status_detail = f"Unable to start sync: {exc}"
                    self._logger.exception("Unable to start sync worker")
                    state = self._state
                    detail = self._status_detail
                    release_lease = True

        if release_lease:
            if sync_lease is not None:
                sync_lease.release()
            if state is not AppState.ERROR:
                return False

        if state is AppState.ERROR:
            self._publish_status(state, detail)
            self._notify("Skin sync failed", detail)
            return False
        return True

    def _sync_rejection_locked(self) -> str | None:
        if not self._started:
            return "The application is still starting."
        if self._stop_event.is_set() or self._state is AppState.STOPPING:
            return "The application is stopping."
        if self._sync_running:
            return "A skin sync is already in progress."
        return None

    def start_manager(self) -> bool:
        """Launch CSLOL Manager, or safely queue it behind an active sync."""

        with self._lock:
            if self._stop_event.is_set():
                return False
            if self._sync_running or self._startup_sync_pending:
                self._manual_manager_launch_pending = True
                queued = True
            else:
                queued = False
        if queued:
            self._notify(
                "CSLOL Manager",
                "Manager launch queued until skin synchronization finishes.",
            )
            return True
        return self._launch_manager_now()

    def _launch_manager_now(self) -> bool:
        """Serialize a manual launch and close the final operation race."""

        with self._manager_launch_lock:
            with self._lock:
                if self._stop_event.is_set():
                    return False
                if self._sync_running or self._startup_sync_pending:
                    self._manual_manager_launch_pending = True
                    return True

            acquired, lease = self._try_acquire_manager_gate()
            if not acquired:
                with self._lock:
                    if self._stop_event.is_set():
                        return False
                    self._manual_manager_launch_pending = True
                owner = None
                if self._operation_gate is not None:
                    owner = self._operation_gate.current_owner
                owner_detail = owner or "another maintenance operation"
                self._notify(
                    "CSLOL Manager",
                    f"Manager launch queued until {owner_detail} finishes.",
                )
                return True

            try:
                with self._lock:
                    if self._stop_event.is_set():
                        return False
                    if self._sync_running or self._startup_sync_pending:
                        self._manual_manager_launch_pending = True
                        return True
                    self._manual_manager_launch_pending = False
                return self._call_launcher()
            finally:
                if lease is not None:
                    lease.release()

    def resume_pending_manager_launches(self) -> bool:
        """Retry manager work queued behind an external operation.

        The external coordinator should call this only after releasing its gate
        lease. The method remains race-safe if a new sync or operation starts
        before the retry acquires the launch boundary.
        """

        return self._resume_pending_manager_launches(include_automatic=True)

    def _resume_pending_manager_launches(self, *, include_automatic: bool) -> bool:
        with self._lock:
            if self._stop_event.is_set() or self._sync_running or self._startup_sync_pending:
                return False
            pending = self._pending_league_pid
            if pending is not None and (
                pending != self._current_league_pid or pending == self._launched_for_league_pid
            ):
                self._pending_league_pid = None
                pending = None
            launch_manual = self._manual_manager_launch_pending

        if include_automatic and pending is not None:
            self._launch_pid_now(pending, deferred=True)
            with self._lock:
                if self._launched_for_league_pid == pending:
                    return True
                if self._pending_league_pid == pending:
                    return False

        if launch_manual:
            return self._launch_manager_now()
        return True

    def _try_acquire_manager_gate(self) -> tuple[bool, OperationLease | None]:
        if self._operation_gate is None:
            return True, None
        lease = self._operation_gate.try_acquire(_MANAGER_GATE_OWNER)
        return lease is not None, lease

    def _call_launcher(self) -> bool:
        try:
            result = self._launcher()
        except Exception as exc:
            self._logger.exception("CSLOL Manager launch failed")
            self._notify("CSLOL Manager", f"Could not start manager: {exc}")
            return False
        if result is False:
            self._notify("CSLOL Manager", "Could not start manager.")
            return False
        return True

    def shutdown(self, timeout_seconds: float | None = None) -> bool:
        """Request cooperative stop and join workers within one total deadline."""

        timeout = self._shutdown_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("timeout_seconds must be positive")

        should_publish = False
        with self._lock:
            if self._state is not AppState.STOPPING:
                self._state = AppState.STOPPING
                self._status_detail = _DEFAULT_STATUS[self._state]
                should_publish = True
            self._stop_event.set()
            workers = tuple(self._sync_threads)
            if self._monitor_thread is not None:
                workers += (self._monitor_thread,)
            state = self._state
            detail = self._status_detail

        if should_publish:
            self._publish_status(state, detail)

        deadline = time.monotonic() + timeout
        current = threading.current_thread()
        for worker in workers:
            if worker is current or not worker.is_alive():
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

        alive = [worker.name for worker in workers if worker is not current and worker.is_alive()]
        if alive:
            names = ", ".join(alive)
            self._logger.warning("Shutdown timed out waiting for: %s", names)
            self._notify(
                "LeagueSkinManagerVN",
                f"Shutdown timed out waiting for: {names}",
            )
            return False
        return True

    def _run_sync(self, lease: OperationLease | None) -> None:
        sync_succeeded = False
        should_resume = False
        try:
            try:
                outcome = self._sync(self._stop_event)
                if outcome is None:
                    outcome = SyncOutcome(
                        AppState.READY,
                        _DEFAULT_STATUS[AppState.READY],
                    )
                elif not isinstance(outcome, SyncOutcome):
                    raise TypeError("sync must return SyncOutcome or None")
            except Exception as exc:
                self._logger.exception("Skin synchronization failed")
                should_resume = self._complete_sync(AppState.ERROR, f"Sync failed: {exc}")
                if not self._stop_event.is_set():
                    self._notify("Skin sync failed", str(exc))
            else:
                sync_succeeded = outcome.state in (AppState.READY, AppState.OFFLINE_READY)
                should_resume = self._complete_sync(outcome.state, outcome.detail)
        finally:
            with self._lock:
                self._sync_threads.discard(threading.current_thread())
            if lease is not None:
                lease.release()

        if should_resume:
            # Release the sync lease before a queued manager process tries to
            # acquire its own short launch lease.
            self._resume_pending_manager_launches(include_automatic=sync_succeeded)

    def _complete_sync(self, state: AppState, detail: str) -> bool:
        with self._lock:
            self._sync_running = False
            if self._stop_event.is_set() or self._state is AppState.STOPPING:
                return False
            sync_succeeded = state in (AppState.READY, AppState.OFFLINE_READY)
            if self._monitor_failure_detail is not None:
                publish_state = AppState.ERROR
                publish_detail = self._monitor_failure_detail
            else:
                publish_state = state
                publish_detail = detail
            self._state = publish_state
            self._status_detail = publish_detail
            if sync_succeeded:
                pending = self._pending_league_pid
                if pending is not None and (
                    pending != self._current_league_pid or pending == self._launched_for_league_pid
                ):
                    self._pending_league_pid = None
        self._publish_status(publish_state, publish_detail)
        return True

    def _run_monitor(self) -> None:
        try:
            self._monitor.run(self._stop_event, self._league_pid_changed)
        except Exception as exc:
            self._logger.exception("League process monitor stopped unexpectedly")
            self._report_monitor_failure(str(exc))
        else:
            if not self._stop_event.is_set():
                self._logger.error("League process monitor returned unexpectedly")
                self._report_monitor_failure("Monitor stopped unexpectedly")

    def _report_monitor_failure(self, message: str) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._state = AppState.ERROR
            self._status_detail = f"Process monitor failed: {message}"
            self._monitor_failure_detail = self._status_detail
            state = self._state
            detail = self._status_detail
        self._publish_status(state, detail)
        self._notify("League process monitor", message)

    def _launch_pid_now(self, pid: int, *, deferred: bool) -> bool:
        """Attempt one PID-bound launch without reserving it while gate-blocked."""

        with self._manager_launch_lock:
            with self._lock:
                if self._stop_event.is_set() or self._current_league_pid != pid:
                    if self._pending_league_pid == pid:
                        self._pending_league_pid = None
                    return False
                if self._launched_for_league_pid == pid:
                    self._pending_league_pid = None
                    return True
                if self._sync_running or self._startup_sync_pending:
                    self._pending_league_pid = pid
                    return False

            acquired, lease = self._try_acquire_manager_gate()
            if not acquired:
                # Do not reserve ``launched_for_league_pid`` here. The migration
                # coordinator will retry this still-live PID after releasing.
                with self._lock:
                    if (
                        not self._stop_event.is_set()
                        and self._current_league_pid == pid
                        and self._launched_for_league_pid != pid
                    ):
                        self._pending_league_pid = pid
                return False

            try:
                with self._lock:
                    if self._stop_event.is_set() or self._current_league_pid != pid:
                        if self._pending_league_pid == pid:
                            self._pending_league_pid = None
                        return False
                    if self._sync_running or self._startup_sync_pending:
                        self._pending_league_pid = pid
                        return False
                    if self._launched_for_league_pid == pid:
                        self._pending_league_pid = None
                        return True
                    # Reserve only after both exclusion boundaries are held. A
                    # launcher failure remains non-retryable for this PID, matching
                    # the controller's established once-per-League-process policy.
                    self._launched_for_league_pid = pid
                    self._pending_league_pid = None
                    self._manual_manager_launch_pending = False
                launched = self._call_launcher()
            finally:
                if lease is not None:
                    lease.release()

        if not launched:
            launch_kind = "Deferred" if deferred else "Automatic"
            self._logger.warning(
                "%s CSLOL Manager launch failed for League PID %s",
                launch_kind,
                pid,
            )
        return launched

    def _league_pid_changed(self, pid: int | None) -> None:
        with self._lock:
            if self._stop_event.is_set():
                return
            self._current_league_pid = pid
            if pid is None:
                self._launched_for_league_pid = None
                self._pending_league_pid = None
                return
            if pid == self._launched_for_league_pid:
                self._pending_league_pid = None
                return
            if self._sync_running or self._startup_sync_pending:
                # Do not launch a manager that may read files while sync is
                # replacing them. Keep only the latest still-running League PID.
                self._pending_league_pid = pid
                return
            # Mark pending before releasing the state lock. The launch lock closes
            # duplicate callback races without claiming the PID prematurely when
            # an external operation owns the shared gate.
            self._pending_league_pid = pid

        self._launch_pid_now(pid, deferred=False)

    def _publish_status(self, state: AppState, detail: str) -> None:
        if self._status_sink is None:
            return
        try:
            self._status_sink(state, detail)
        except Exception:
            self._logger.exception("Status sink failed")

    def _notify(self, title: str, message: str) -> None:
        if self._notify_sink is None:
            return
        try:
            self._notify_sink(title, message)
        except Exception:
            self._logger.exception("Notification sink failed")


__all__ = [
    "AppController",
    "AppState",
    "ManagerLauncher",
    "NotifySink",
    "ProcessMonitor",
    "StatusSink",
    "SyncCallable",
    "SyncOutcome",
]
