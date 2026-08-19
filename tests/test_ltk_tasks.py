from __future__ import annotations

import logging
import time
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

from league_skin_manager.ltk_cleanup import (
    LtkSkinCleanupError,
    LtkSkinCleanupResult,
    LtkSkinCleanupService,
)
from league_skin_manager.ltk_companion import (
    LtkCompanion,
    LtkCompanionResult,
    LtkCompanionStatus,
    LtkPreparationResult,
    LtkPreparationStatus,
    LtkRelease,
    LtkReleaseAsset,
    LtkVersion,
)
from league_skin_manager.ltk_migration import (
    BaselineStatus,
    LtkMigrationService,
    LtkReconcileError,
    ReconcileBlockedError,
    ReconcileIssue,
    ReconcileProgress,
    ReconcileResult,
)
from league_skin_manager.ltk_tasks import LtkTaskCoordinator
from league_skin_manager.operation_gate import OperationGate


def release() -> LtkRelease:
    version = LtkVersion(1, 11, 0)
    return LtkRelease(
        version,
        LtkReleaseAsset(
            "LTK.Manager_1.11.0_x64-setup.exe",
            "https://github.com/LeagueToolkit/ltk-manager/releases/download/v1.11.0/"
            "LTK.Manager_1.11.0_x64-setup.exe",
            1,
            f"sha256:{'a' * 64}",
        ),
    )


class FakeCompanion:
    def __init__(self, tmp_path: Path, *, block_prepare: bool = False) -> None:
        self.release = release()
        self.prepare_started = Event()
        self.prepare_release = Event()
        if not block_prepare:
            self.prepare_release.set()
        self.prepare_calls = 0
        self.start_calls = 0
        self.closed = 0
        self.installer = tmp_path / self.release.asset.name
        self.start_result = LtkCompanionResult(
            LtkCompanionStatus.LAUNCHED_CURRENT,
            self.release.version,
            tmp_path / "ltk-manager.exe",
            self.release,
        )

    def prepare(self, cancel_event: Event | None = None) -> LtkPreparationResult:
        self.prepare_calls += 1
        self.prepare_started.set()
        while not self.prepare_release.wait(0.01):
            if cancel_event is not None and cancel_event.is_set():
                from league_skin_manager.ltk_companion import LtkCancelled

                raise LtkCancelled("cancelled")
        return LtkPreparationResult(
            LtkPreparationStatus.INSTALLER_READY,
            self.release,
            None,
            self.installer,
        )

    def start(self, _cancel_event: Event | None = None) -> LtkCompanionResult:
        self.start_calls += 1
        return self.start_result

    def close(self) -> None:
        self.closed += 1


class FakeReconciler:
    def __init__(self, tmp_path: Path) -> None:
        self.calls = 0
        self.inspect_calls = 0
        self.error: LtkReconcileError | None = None
        self.baseline = BaselineStatus(expected=3, present=1, extra=0)
        self.result = ReconcileResult(
            storage_dir=tmp_path / "ltk",
            archives_dir=tmp_path / "ltk" / "archives",
            report_path=tmp_path / "report.json",
            status="completed",
            expected=3,
            added=2,
            removed=1,
            unchanged=1,
            toggles_cleared=4,
            issues=(),
        )

    def reconcile(
        self,
        *,
        cancel_event: object | None = None,
        progress: Any = None,
    ) -> ReconcileResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if progress is not None:
            progress(ReconcileProgress("queueing", 1, 3, "Shaco"))
        return self.result

    def inspect_baseline(self) -> BaselineStatus:
        self.inspect_calls += 1
        return self.baseline


class FakeCleanup:
    def __init__(self, tmp_path: Path) -> None:
        self.calls = 0
        self.error: LtkSkinCleanupError | None = None
        self.result = LtkSkinCleanupResult(
            storage_dir=tmp_path / "ltk",
            library_mods=7,
            archives=6,
            metadata_directories=7,
            profile_directories=2,
            reports_removed=True,
            library_reset=True,
        )

    def remove_all(self) -> LtkSkinCleanupResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


def wait_until(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def coordinator(
    tmp_path: Path,
    companion: FakeCompanion,
    reconciler: FakeReconciler,
    *,
    gate: OperationGate | None = None,
    running: Any = lambda: False,
    installed: Any = lambda: True,
    cleanup: FakeCleanup | None = None,
    library_changed: Any = None,
) -> tuple[LtkTaskCoordinator, list[tuple[str, str]], list[tuple[str, bool]], list[str]]:
    notifications: list[tuple[str, str]] = []
    statuses: list[tuple[str, bool]] = []
    resumed: list[str] = []
    value = LtkTaskCoordinator(
        companion=cast(LtkCompanion, companion),
        reconciler=cast(LtkMigrationService, reconciler),
        cleanup=cast(LtkSkinCleanupService, cleanup or FakeCleanup(tmp_path)),
        operation_gate=gate or OperationGate(),
        ltk_is_running=running,
        ltk_is_installed=installed,
        resume_cslol_launches=lambda: resumed.append("resume"),
        notify_sink=lambda title, message: notifications.append((title, message)),
        status_sink=lambda detail, active: statuses.append((detail, active)),
        library_state_changed_sink=library_changed,
        logger=logging.getLogger("test.ltk_tasks"),
    )
    return value, notifications, statuses, resumed


def test_start_checks_the_release_without_launching(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    value, _notifications, statuses, _resumed = coordinator(tmp_path, companion, reconciler)

    assert value.start() is True
    wait_until(lambda: companion.prepare_calls == 1 and "cached" in statuses[-1][0])

    assert companion.start_calls == 0
    assert reconciler.calls == 0
    assert value.shutdown(1.0) is True
    assert companion.closed == 1
    assert value.shutdown(1.0) is True
    assert companion.closed == 1


def test_explicit_start_is_coalesced_behind_the_release_check(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path, block_prepare=True)
    value, notifications, statuses, _resumed = coordinator(
        tmp_path, companion, FakeReconciler(tmp_path)
    )
    assert value.start()
    assert companion.prepare_started.wait(1.0)

    assert value.request_start() is True
    assert value.request_start() is True
    companion.prepare_release.set()
    wait_until(lambda: companion.start_calls == 1)

    assert any("already queued" in detail for detail, _active in statuses)
    assert notifications[-1] == ("LTK Manager", "Opened LTK Manager v1.11.0.")
    assert value.shutdown(1.0)


def test_running_ltk_blocks_a_duplicate_launch(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    value, notifications, _statuses, _resumed = coordinator(
        tmp_path,
        companion,
        FakeReconciler(tmp_path),
        running=lambda: True,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_start()
    wait_until(lambda: bool(notifications))

    assert companion.start_calls == 0
    assert "already running" in notifications[-1][1]
    assert value.shutdown(1.0)


def test_rebuild_waits_for_the_gate_reports_progress_and_resumes_cslol(
    tmp_path: Path,
) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    library_changes: list[str] = []
    value, notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        reconciler,
        gate=gate,
        library_changed=lambda: library_changes.append("changed"),
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild()
    wait_until(lambda: any("waiting" in detail for detail, _active in statuses))
    assert reconciler.calls == 0

    blocker.release()
    wait_until(lambda: reconciler.calls == 1 and resumed == ["resume"])

    assert companion.start_calls == 0
    assert any("queueing 1/3 - Shaco" in detail for detail, _active in statuses)
    assert notifications[-1][0] == "LTK skin library updated"
    assert "2 queued" in notifications[-1][1]
    assert "1 removed" in notifications[-1][1]
    assert library_changes == ["changed"]
    assert value.reconcile_active is False
    assert value.shutdown(1.0)


def test_automatic_rebuild_is_skipped_when_ltk_is_not_installed(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    value, notifications, statuses, _resumed = coordinator(
        tmp_path,
        companion,
        reconciler,
        installed=lambda: False,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild(automatic=True)
    wait_until(lambda: any("LTK is not installed" in detail for detail, _a in statuses))

    assert reconciler.calls == 0
    assert notifications == []
    assert value.shutdown(1.0)


def test_an_unchanged_automatic_rebuild_stays_quiet(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.result = replace(
        reconciler.result, added=0, removed=0, toggles_cleared=0, unchanged=3
    )
    value, notifications, statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild(automatic=True)
    wait_until(lambda: resumed == ["resume"])

    assert reconciler.calls == 1
    assert notifications == []
    assert any("already current" in detail for detail, _active in statuses)
    assert value.shutdown(1.0)


def test_a_manual_rebuild_always_reports_even_when_unchanged(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.result = replace(
        reconciler.result, added=0, removed=0, toggles_cleared=0, unchanged=3
    )
    value, notifications, _statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild()
    wait_until(lambda: resumed == ["resume"])

    assert notifications[-1][0] == "LTK skin library updated"
    assert value.shutdown(1.0)


def test_a_blocked_rebuild_defers_quietly_and_retries_later(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.error = ReconcileBlockedError("Close LTK Manager before rebuilding")
    value, notifications, statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild(automatic=True)
    wait_until(lambda: any("rebuild deferred" in detail for detail, _a in statuses))
    wait_until(lambda: resumed == ["resume"])

    assert notifications == []
    assert reconciler.calls == 1

    reconciler.error = None
    assert value.retry_deferred_rebuild() is True
    wait_until(lambda: reconciler.calls == 2 and len(resumed) == 2)

    assert value.retry_deferred_rebuild() is False
    assert value.shutdown(1.0)


def test_a_blocked_result_also_marks_the_rebuild_deferred(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.result = replace(
        reconciler.result,
        status="blocked",
        issues=(ReconcileIssue(reason="Close LTK Manager before rebuilding"),),
    )
    value, _notifications, statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild(automatic=True)
    wait_until(lambda: resumed == ["resume"])
    wait_until(lambda: any("rebuild deferred" in detail for detail, _a in statuses))

    reconciler.result = replace(reconciler.result, status="completed", issues=())
    assert value.retry_deferred_rebuild() is True
    assert value.shutdown(1.0)


def test_a_failed_rebuild_is_status_only_when_automatic(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.error = LtkReconcileError("package cache unavailable")
    value, notifications, statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild(automatic=True)
    wait_until(lambda: resumed == ["resume"])
    wait_until(lambda: any("rebuild failed" in detail for detail, _a in statuses))

    assert notifications == []
    assert value.retry_deferred_rebuild() is False
    assert value.shutdown(1.0)


def test_a_cancelled_rebuild_reports_a_partial_result(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.result = replace(reconciler.result, status="cancelled")
    library_changes: list[str] = []
    value, notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        reconciler,
        library_changed=lambda: library_changes.append("changed"),
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild()
    wait_until(lambda: resumed == ["resume"])

    assert any("rebuild cancelled" in detail for detail, _active in statuses)
    assert notifications == []
    assert library_changes == ["changed"]
    assert value.shutdown(1.0)


def test_cancel_a_queued_rebuild_leaves_the_service_untouched(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, _notifications, statuses, resumed = coordinator(
        tmp_path, companion, reconciler, gate=gate
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_rebuild()
    wait_until(lambda: value.reconcile_active)

    assert value.cancel_rebuild() is True
    wait_until(lambda: any("cancelled before" in detail for detail, _a in statuses))

    assert reconciler.calls == 0
    assert resumed == []
    blocker.release()
    assert value.shutdown(1.0)


def test_cancel_without_a_rebuild_and_invalid_shutdown_timeout(tmp_path: Path) -> None:
    value, _notifications, _statuses, _resumed = coordinator(
        tmp_path, FakeCompanion(tmp_path), FakeReconciler(tmp_path)
    )
    assert value.cancel_rebuild() is False
    with pytest.raises(ValueError, match="positive"):
        value.shutdown(0)
    assert value.shutdown(1.0)


def test_a_duplicate_automatic_rebuild_is_rejected(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, _notifications, _statuses, _resumed = coordinator(
        tmp_path, companion, FakeReconciler(tmp_path), gate=gate
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_rebuild()
    wait_until(lambda: value.reconcile_active)

    assert value.request_rebuild(automatic=True) is False

    assert value.cancel_rebuild()
    blocker.release()
    assert value.shutdown(1.0)


def test_report_failure_is_surfaced_without_hiding_the_outcome(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    reconciler = FakeReconciler(tmp_path)
    reconciler.result = replace(reconciler.result, report_error="report disk full")
    value, notifications, statuses, resumed = coordinator(tmp_path, companion, reconciler)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild()
    wait_until(lambda: resumed == ["resume"])

    assert any("report unavailable" in detail for detail, _active in statuses)
    assert notifications[-1][0] == "LTK skin library updated"
    assert value.shutdown(1.0)


def test_cleanup_waits_for_the_gate_and_resumes_cslol(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    cleanup = FakeCleanup(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    library_changes: list[str] = []
    value, notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        FakeReconciler(tmp_path),
        gate=gate,
        cleanup=cleanup,
        library_changed=lambda: library_changes.append("changed"),
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_cleanup()
    assert value.request_cleanup()
    wait_until(lambda: value.cleanup_active)
    assert cleanup.calls == 0

    blocker.release()
    wait_until(lambda: any("removed every skin" in detail for detail, _a in statuses))

    assert resumed == ["resume"]
    assert value.cleanup_active is False
    assert notifications[-1][0] == "All skins removed from LTK"
    assert "next rebuild will restore" in notifications[-1][1]
    assert library_changes == ["changed"]
    assert value.shutdown(1.0)


def test_cleanup_failure_is_reported(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    cleanup = FakeCleanup(tmp_path)
    cleanup.error = LtkSkinCleanupError("cleanup transaction failed")
    value, notifications, statuses, resumed = coordinator(
        tmp_path, companion, FakeReconciler(tmp_path), cleanup=cleanup
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_cleanup()
    wait_until(lambda: resumed == ["resume"])

    assert cleanup.calls == 1
    assert notifications[-1][0] == "Remove all skins from LTK"
    assert any("skin removal not completed" in detail for detail, _a in statuses)
    assert value.shutdown(1.0)


def test_cleanup_and_rebuild_reject_each_other(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, notifications, _statuses, _resumed = coordinator(
        tmp_path,
        companion,
        FakeReconciler(tmp_path),
        gate=gate,
        cleanup=FakeCleanup(tmp_path),
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_rebuild()
    wait_until(lambda: value.reconcile_active)

    assert value.request_cleanup() is False
    assert "library rebuild" in notifications[-1][1]

    assert value.cancel_rebuild()
    blocker.release()
    wait_until(lambda: not value.reconcile_active)
    assert value.shutdown(1.0)


def test_library_state_sink_failure_does_not_change_the_outcome(
    tmp_path: Path,
    caplog: Any,
) -> None:
    def fail_refresh() -> None:
        raise RuntimeError("refresh failed")

    companion = FakeCompanion(tmp_path)
    value, notifications, _statuses, resumed = coordinator(
        tmp_path,
        companion,
        FakeReconciler(tmp_path),
        library_changed=fail_refresh,
    )
    caplog.set_level(logging.ERROR, logger="test.ltk_tasks")
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_rebuild()
    wait_until(lambda: resumed == ["resume"])

    assert notifications[-1][0] == "LTK skin library updated"
    assert "LTK library-state change sink failed" in caplog.text
    assert value.shutdown(1.0)
