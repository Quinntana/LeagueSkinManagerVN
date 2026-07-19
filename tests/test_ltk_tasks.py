from __future__ import annotations

import logging
import time
from pathlib import Path
from threading import Event
from typing import Any, cast

from league_skin_manager.ltk_cleanup import LtkSkinCleanupResult, LtkSkinCleanupService
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
    LtkMigrationService,
    MigrationIssue,
    MigrationProgress,
    MigrationResult,
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


class FakeMigration:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[Path] = []
        self.forget_calls = 0
        self.result = MigrationResult(
            source_dir=tmp_path / "installed",
            storage_dir=tmp_path / "ltk",
            archives_dir=tmp_path / "ltk" / "archives",
            report_path=tmp_path / "report.json",
            status="completed",
            discovered=3,
            queued=2,
            skipped=1,
            failed=0,
            reused_cache=2,
            packaged=1,
            issues=(),
        )

    def migrate(
        self,
        source: Path,
        *,
        cancel_event: object | None = None,
        progress: Any = None,
    ) -> MigrationResult:
        self.calls.append(source)
        if progress is not None:
            progress(MigrationProgress("packaging", 1, 3, "Shaco"))
        return self.result

    def forget_history(self) -> None:
        self.forget_calls += 1


class FakeCleanup:
    def __init__(self, tmp_path: Path) -> None:
        self.calls = 0
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
    migration: FakeMigration,
    *,
    gate: OperationGate | None = None,
    running: Any = lambda: False,
    cleanup: FakeCleanup | None = None,
) -> tuple[LtkTaskCoordinator, list[tuple[str, str]], list[tuple[str, bool]], list[str]]:
    notifications: list[tuple[str, str]] = []
    statuses: list[tuple[str, bool]] = []
    resumed: list[str] = []
    value = LtkTaskCoordinator(
        companion=cast(LtkCompanion, companion),
        migration=cast(LtkMigrationService, migration),
        cleanup=cast(LtkSkinCleanupService, cleanup or FakeCleanup(tmp_path)),
        operation_gate=gate or OperationGate(),
        ltk_is_running=running,
        resume_cslol_launches=lambda: resumed.append("resume"),
        notify_sink=lambda title, message: notifications.append((title, message)),
        status_sink=lambda detail, active: statuses.append((detail, active)),
        logger=logging.getLogger("test.ltk_tasks"),
    )
    return value, notifications, statuses, resumed


def test_start_prepares_latest_release_without_launching(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    value, _notifications, statuses, _resumed = coordinator(
        tmp_path,
        companion,
        migration,
    )

    assert value.start() is True
    wait_until(lambda: companion.prepare_calls == 1 and "cached" in statuses[-1][0])

    assert companion.start_calls == 0
    assert migration.calls == []
    assert value.shutdown(1.0) is True
    assert companion.closed == 1
    assert value.shutdown(1.0) is True
    assert companion.closed == 1


def test_explicit_start_is_coalesced_behind_automatic_prepare(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path, block_prepare=True)
    value, notifications, statuses, _resumed = coordinator(
        tmp_path,
        companion,
        FakeMigration(tmp_path),
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


def test_running_legacy_or_official_ltk_blocks_a_duplicate_launch(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    value, notifications, _statuses, _resumed = coordinator(
        tmp_path,
        companion,
        FakeMigration(tmp_path),
        running=lambda: True,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_start()
    wait_until(lambda: bool(notifications))

    assert companion.start_calls == 0
    assert "legacy LTK app" in notifications[-1][1]
    assert value.shutdown(1.0)


def test_prepare_completion_keeps_queued_migration_cancellable(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path, block_prepare=True)
    migration = FakeMigration(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, _notifications, statuses, _resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
    )
    assert value.start()
    assert companion.prepare_started.wait(1.0)
    assert value.request_migration(tmp_path / "installed")

    companion.prepare_release.set()
    wait_until(lambda: any("cached" in detail for detail, _active in statuses))

    cached_status = next(item for item in statuses if "cached" in item[0])
    assert cached_status[1] is True
    assert value.cancel_migration()
    blocker.release()
    assert value.shutdown(1.0)


def test_migration_waits_for_gate_reports_progress_launches_ltk_and_resumes_cslol(
    tmp_path: Path,
) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    source = tmp_path / "cslol-manager"
    assert value.request_migration(source)
    wait_until(lambda: any("waiting" in detail for detail, _active in statuses))
    assert migration.calls == []

    blocker.release()
    wait_until(lambda: migration.calls == [source] and resumed == ["resume"])

    assert companion.start_calls == 1
    assert any("packaging 1/3 - Shaco" in detail for detail, _active in statuses)
    assert notifications[-1][0] == "LTK migration complete"
    assert "2 queued, 1 already queued, 0 failed" in notifications[-1][1]
    assert value.migration_active is False
    assert value.shutdown(1.0)


def test_cancel_queued_migration_leaves_gate_and_service_untouched(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, _notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_migration(tmp_path / "installed")
    wait_until(lambda: value.migration_active)
    assert value.cancel_migration() is True
    wait_until(lambda: any("cancelled before" in detail for detail, _active in statuses))

    assert migration.calls == []
    assert resumed == []
    blocker.release()
    assert value.shutdown(1.0)


def test_blocked_partial_migration_does_not_launch_ltk(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    migration.result = MigrationResult(
        source_dir=tmp_path / "installed",
        storage_dir=tmp_path / "ltk",
        archives_dir=tmp_path / "ltk" / "archives",
        report_path=tmp_path / "blocked-report.json",
        status="blocked",
        discovered=3,
        queued=1,
        skipped=0,
        failed=0,
        reused_cache=1,
        packaged=0,
        issues=(
            MigrationIssue(
                source=tmp_path / "installed",
                reason="Close LTK Manager before migrating skins",
            ),
        ),
    )
    value, notifications, statuses, resumed = coordinator(tmp_path, companion, migration)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_migration(tmp_path / "installed")
    wait_until(lambda: bool(resumed))

    assert companion.start_calls == 0
    assert "Close LTK Manager" in notifications[-1][1]
    assert statuses[-1][1] is False
    assert value.shutdown(1.0)


def test_cancel_without_migration_and_invalid_shutdown_timeout(tmp_path: Path) -> None:
    import pytest

    value, _notifications, _statuses, _resumed = coordinator(
        tmp_path,
        FakeCompanion(tmp_path),
        FakeMigration(tmp_path),
    )
    assert value.cancel_migration() is False
    with pytest.raises(ValueError, match="positive"):
        value.shutdown(0)
    assert value.shutdown(1.0)


def test_explicit_history_reset_is_serialized_and_notified(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    value, notifications, statuses, _resumed = coordinator(tmp_path, companion, migration)
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_history_reset()
    assert value.request_history_reset()
    wait_until(lambda: migration.forget_calls == 1)

    assert "packages may be requeued" in statuses[-1][0]
    assert notifications[-1][0] == "LTK migration history"
    assert value.shutdown(1.0)


def test_history_reset_is_rejected_while_migration_is_waiting(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, notifications, _statuses, _resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_migration(tmp_path / "installed")
    wait_until(lambda: value.migration_active)

    assert value.request_history_reset() is False
    assert "active migration" in notifications[-1][1]
    assert value.cancel_migration()
    blocker.release()
    assert value.shutdown(1.0)


def test_explicit_cleanup_waits_for_gate_clears_history_and_resumes_cslol(
    tmp_path: Path,
) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    cleanup = FakeCleanup(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, notifications, statuses, resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
        cleanup=cleanup,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)

    assert value.request_cleanup()
    assert value.request_cleanup()
    wait_until(lambda: value.cleanup_active)
    assert cleanup.calls == 0

    blocker.release()
    wait_until(lambda: cleanup.calls == 1 and migration.forget_calls == 1)

    assert resumed == ["resume"]
    assert value.cleanup_active is False
    assert any("removed all LTK skins (7 package(s))" in item[0] for item in statuses)
    assert notifications[-1][0] == "All LTK skins removed"
    assert "LTK application were preserved" in notifications[-1][1]
    assert value.shutdown(1.0)


def test_cleanup_and_migration_requests_reject_each_other(tmp_path: Path) -> None:
    companion = FakeCompanion(tmp_path)
    migration = FakeMigration(tmp_path)
    cleanup = FakeCleanup(tmp_path)
    gate = OperationGate()
    blocker = gate.try_acquire("skin synchronization")
    assert blocker is not None
    value, notifications, _statuses, _resumed = coordinator(
        tmp_path,
        companion,
        migration,
        gate=gate,
        cleanup=cleanup,
    )
    assert value.start()
    wait_until(lambda: companion.prepare_calls == 1)
    assert value.request_migration(tmp_path / "installed")
    wait_until(lambda: value.migration_active)

    assert value.request_cleanup() is False
    assert "active CSLOL-to-LTK port" in notifications[-1][1]
    assert value.cancel_migration()
    blocker.release()
    wait_until(lambda: not value.migration_active)
    assert value.shutdown(1.0)
