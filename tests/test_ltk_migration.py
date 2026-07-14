from __future__ import annotations

import json
import shutil
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager import ltk_migration as migration_module
from league_skin_manager.atomic import atomic_write_bytes as real_atomic_write_bytes
from league_skin_manager.ltk_migration import (
    LtkMigrationService,
    MigrationBlockedError,
    MigrationHistoryError,
    MigrationProgress,
    MigrationSourceError,
)
from league_skin_manager.skin_installer import (
    extract_fantome,
    git_blob_sha,
    inspect_extracted_mod,
    managed_directory_name,
)


def create_fantome(path: Path, *, name: str = "Test skin", wad: bytes = b"wad") -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META/info.json", json.dumps({"Name": name}))
        archive.writestr("WAD/Test.wad.client", wad)
    return path


def create_live_mod(installed: Path, directory: str, *, wad: bytes = b"wad") -> Path:
    mod = installed / directory
    (mod / "META").mkdir(parents=True)
    (mod / "META" / "info.json").write_text(
        json.dumps({"Name": directory}),
        encoding="utf-8",
    )
    (mod / "WAD").mkdir()
    (mod / "WAD" / "Test.wad.client").write_bytes(wad)
    return mod


def make_service(tmp_path: Path, **options: Any) -> LtkMigrationService:
    return LtkMigrationService(
        tmp_path / "state" / "managed_skins.json",
        tmp_path / "cache" / "packages",
        ltk_app_data_dir=tmp_path / "ltk-data",
        report_dir=tmp_path / "reports",
        **options,
    )


def create_managed_mod(tmp_path: Path) -> tuple[Path, Path, Path]:
    installed = tmp_path / "cslol-manager" / "installed"
    installed.mkdir(parents=True)
    source_path = "skins/Ahri/Foxfire Ahri.fantome"
    directory = managed_directory_name("Ahri", "Foxfire Ahri", source_path)
    archive = create_fantome(tmp_path / "skin.fantome", name="Foxfire Ahri")
    live = installed / directory
    extract_fantome(archive, live)
    fingerprint = inspect_extracted_mod(live)
    assert fingerprint is not None
    source_sha = git_blob_sha(archive)
    cache = tmp_path / "cache" / "packages" / f"{source_sha}.fantome"
    cache.parent.mkdir(parents=True)
    shutil.copyfile(archive, cache)
    state = {
        "schema_version": 1,
        "transaction_id": "test",
        "source_commit": "a" * 40,
        "patch": "16.13.1",
        "entries": [
            {
                "champion": "Ahri",
                "name": "Foxfire Ahri",
                "source_path": source_path,
                "source_sha": source_sha,
                "size": archive.stat().st_size,
                "directory": directory,
                "content_sha256": fingerprint.sha256,
            }
        ],
    }
    state_path = tmp_path / "state" / "managed_skins.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return installed, live, cache


def queued_archives(path: Path) -> list[Path]:
    return sorted(path.glob("*.fantome"))


def test_normalizes_cslol_root_or_direct_installed_folder(tmp_path: Path) -> None:
    root = tmp_path / "cslol-manager"
    installed = root / "installed"
    installed.mkdir(parents=True)
    service = make_service(tmp_path)

    assert service.normalize_source(root) == installed
    assert service.normalize_source(installed) == installed

    invalid = tmp_path / "not-cslol"
    invalid.mkdir()
    with pytest.raises(MigrationSourceError, match="root folder"):
        service.normalize_source(invalid)
    with pytest.raises(MigrationSourceError, match="safe directory"):
        service.normalize_source(tmp_path / "missing")


def test_honors_only_absolute_ltk_custom_storage_path(tmp_path: Path) -> None:
    app_data = tmp_path / "ltk-data"
    app_data.mkdir()
    custom = tmp_path / "external-ltk-storage"
    settings = app_data / "settings.json"
    settings.write_text(json.dumps({"modStoragePath": str(custom)}), encoding="utf-8")
    service = LtkMigrationService(
        tmp_path / "state.json",
        tmp_path / "cache",
        ltk_app_data_dir=app_data,
    )

    assert service.resolve_storage_dir() == custom

    settings.write_text(json.dumps({"modStoragePath": "relative/storage"}), encoding="utf-8")
    assert service.resolve_storage_dir() == app_data
    settings.write_text("not-json", encoding="utf-8")
    assert service.resolve_storage_dir() == app_data


@pytest.mark.parametrize("running_manager", ["cslol", "ltk"])
def test_running_manager_blocks_before_creating_ltk_storage(
    tmp_path: Path,
    running_manager: str,
) -> None:
    installed = tmp_path / "cslol-manager" / "installed"
    installed.mkdir(parents=True)
    create_live_mod(installed, "custom")
    service = make_service(
        tmp_path,
        cslol_is_running=lambda: running_manager == "cslol",
        ltk_is_running=lambda: running_manager == "ltk",
    )

    with pytest.raises(MigrationBlockedError, match="Close"):
        service.migrate(installed)

    assert not (tmp_path / "ltk-data").exists()
    assert not (tmp_path / "reports").exists()


def test_process_lookup_failure_blocks_closed(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()

    def fail_lookup() -> bool:
        raise OSError("process snapshot unavailable")

    service = make_service(tmp_path, ltk_is_running=fail_lookup)
    with pytest.raises(MigrationBlockedError, match="verify"):
        service.migrate(installed)


def test_verified_managed_cache_is_reused_without_editing_ltk_library(
    tmp_path: Path,
) -> None:
    installed, _live, cache = create_managed_mod(tmp_path)
    storage = tmp_path / "ltk-data"
    storage.mkdir()
    library = storage / "library.json"
    library.write_bytes(b"do-not-touch")
    service = make_service(tmp_path)
    events: list[MigrationProgress] = []

    result = service.migrate(installed.parent, progress=events.append)

    archives = queued_archives(storage / "archives")
    assert result.status == "completed"
    assert result.queued == 1
    assert result.reused_cache == 1
    assert result.packaged == 0
    assert len(archives) == 1
    assert archives[0].read_bytes() == cache.read_bytes()
    assert library.read_bytes() == b"do-not-touch"
    assert events[-1].phase == "completed"
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["queued"] == 1
    assert report["status"] == "completed"
    assert not list(result.report_path.parent.glob("*.tmp"))


def test_tampered_managed_source_is_packaged_from_live_tree_not_cache(
    tmp_path: Path,
) -> None:
    installed, live, cache = create_managed_mod(tmp_path)
    (live / "WAD" / "Test.wad.client").write_bytes(b"live-user-edit")
    service = make_service(tmp_path)

    result = service.migrate(installed)

    archives = queued_archives(result.archives_dir)
    assert result.queued == 1
    assert result.reused_cache == 0
    assert result.packaged == 1
    assert len(archives) == 1
    assert archives[0].read_bytes() != cache.read_bytes()
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.read("WAD/Test.wad.client") == b"live-user-edit"


def test_live_packaging_is_deterministic_and_migration_is_idempotent(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "cslol-manager" / "installed"
    installed.mkdir(parents=True)
    create_live_mod(installed, "Handmade Skin", wad=b"custom-wad")
    first = make_service(tmp_path)

    first_result = first.migrate(installed)
    first_archive = queued_archives(first_result.archives_dir)[0]
    original_bytes = first_archive.read_bytes()
    original_name = first_archive.name

    repeated = first.migrate(installed)
    assert repeated.queued == 0
    assert repeated.skipped == 1
    assert len(queued_archives(repeated.archives_dir)) == 1

    second_storage = tmp_path / "ltk-data-2"
    second = LtkMigrationService(
        tmp_path / "missing-state.json",
        tmp_path / "missing-cache",
        ltk_app_data_dir=second_storage,
        report_dir=tmp_path / "reports-2",
    )
    second_result = second.migrate(installed)
    second_archive = queued_archives(second_result.archives_dir)[0]
    assert second_archive.name == original_name
    assert second_archive.read_bytes() == original_bytes


def test_deduplicates_same_bytes_already_queued_as_modpkg(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom")
    first = make_service(tmp_path)
    first_result = first.migrate(installed)
    generated = queued_archives(first_result.archives_dir)[0]
    duplicate = first_result.archives_dir / "already-there.modpkg"
    generated.replace(duplicate)
    first.migration_state_path.unlink()

    repeated = first.migrate(installed)

    assert repeated.skipped == 1
    assert repeated.queued == 0
    assert duplicate.is_file()
    assert queued_archives(repeated.archives_dir) == []

    duplicate.unlink()
    after_consumption = first.migrate(installed)
    assert after_consumption.skipped == 1
    assert after_consumption.queued == 0
    assert queued_archives(after_consumption.archives_dir) == []


def test_consumed_archive_is_skipped_until_explicit_history_reset(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom", wad=b"history-test")
    ledger = tmp_path / "vn-owned-state" / "migration.json"
    service = LtkMigrationService(
        tmp_path / "managed.json",
        tmp_path / "cache",
        ltk_app_data_dir=tmp_path / "ltk-data",
        report_dir=tmp_path / "reports",
        migration_state_path=ledger,
    )

    first = service.migrate(installed)
    archive = queued_archives(first.archives_dir)[0]
    archive.unlink()  # LTK consumes archives after importing them.

    repeated = service.migrate(installed)
    assert repeated.queued == 0
    assert repeated.skipped == 1
    assert queued_archives(repeated.archives_dir) == []
    state = json.loads(ledger.read_text(encoding="utf-8"))
    assert state["schema_version"] == 1
    assert len(state["packages"]) == 1
    record = next(iter(state["packages"].values()))
    assert record["source"].endswith("custom")
    assert record["name"] == "custom"
    assert record["queued_at"]
    assert not list(ledger.parent.glob("*.tmp"))

    service.forget_history()
    assert json.loads(ledger.read_text(encoding="utf-8"))["packages"] == {}
    after_reset = service.migrate(installed)
    assert after_reset.queued == 1
    assert after_reset.skipped == 0


def test_malformed_or_oversized_history_fails_before_ltk_mutation(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom")
    ledger = tmp_path / "state" / "ltk_migration_state.json"
    ledger.parent.mkdir()
    ledger.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "packages": {"not-a-sha256": {"source": "x", "name": "x", "queued_at": "x"}},
            }
        ),
        encoding="utf-8",
    )
    service = make_service(tmp_path)

    with pytest.raises(MigrationHistoryError, match="invalid package digest"):
        service.migrate(installed)
    assert not (tmp_path / "ltk-data").exists()

    # Explicit reset remains available to recover a corrupt regular ledger.
    service.forget_history()
    assert service.migrate(installed).queued == 1

    other = tmp_path / "oversized" / "installed"
    other.mkdir(parents=True)
    create_live_mod(other, "custom")
    oversized_ledger = tmp_path / "oversized-state.json"
    oversized_ledger.write_bytes(b"x" * 65)
    limited = LtkMigrationService(
        tmp_path / "other-managed.json",
        tmp_path / "other-cache",
        ltk_app_data_dir=tmp_path / "other-ltk",
        migration_state_path=oversized_ledger,
        max_history_bytes=64,
    )
    with pytest.raises(MigrationHistoryError, match="size limit"):
        limited.migrate(other)
    assert not (tmp_path / "other-ltk").exists()


class _CancelAfterChecks:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.calls = 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls >= self.limit


def test_cancellation_removes_partial_archive_and_writes_partial_report(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "large", wad=b"x" * (4 * 1024 * 1024))
    cancelled = _CancelAfterChecks(limit=10)
    service = make_service(tmp_path)

    result = service.migrate(installed, cancel_event=cancelled)

    assert result.cancelled
    assert result.queued == 0
    assert result.report_path.is_file()
    assert not list(result.archives_dir.glob("*.partial"))
    assert not queued_archives(result.archives_dir)


def test_cancellation_keeps_each_completed_queue_durable_in_history(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "first", wad=b"first")
    create_live_mod(installed, "second", wad=b"second")
    cancelled = threading.Event()

    def cancel_after_first(event: MigrationProgress) -> None:
        if event.phase == "migrating" and event.completed == 1:
            cancelled.set()

    service = make_service(tmp_path)
    partial = service.migrate(
        installed,
        cancel_event=cancelled,
        progress=cancel_after_first,
    )

    assert partial.cancelled
    assert partial.queued == 1
    state = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(state["packages"]) == 1
    queued_archives(partial.archives_dir)[0].unlink()

    resumed = service.migrate(installed)
    assert resumed.skipped == 1
    assert resumed.queued == 1
    final_state = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(final_state["packages"]) == 2


def test_ledger_write_failure_rolls_back_newly_queued_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom")
    service = make_service(tmp_path)

    def fail_history_write(_path: Path, _value: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(migration_module, "atomic_write_bytes", fail_history_write)

    with pytest.raises(MigrationHistoryError, match="persist"):
        service.migrate(installed)

    assert not queued_archives(tmp_path / "ltk-data" / "archives")
    assert not list((tmp_path / "ltk-data" / "archives").glob("*.partial"))


def test_many_packages_use_bounded_history_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    for index in range(130):
        create_live_mod(
            installed,
            f"mod-{index:03d}",
            wad=f"wad-{index}".encode(),
        )
    service = make_service(tmp_path)
    real_atomic_write = real_atomic_write_bytes
    history_writes = 0

    def count_history_write(path: Path, value: bytes) -> None:
        nonlocal history_writes
        if path == service.migration_state_path:
            history_writes += 1
        real_atomic_write(path, value)

    monkeypatch.setattr(migration_module, "atomic_write_bytes", count_history_write)

    result = service.migrate(installed)

    assert result.queued == 130
    assert history_writes == 3  # 64, 128, and the final two records.
    state = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(state["packages"]) == 130


def test_failed_checkpoint_rolls_back_only_work_after_last_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    for index in range(5):
        create_live_mod(installed, f"mod-{index}", wad=f"wad-{index}".encode())
    service = LtkMigrationService(
        tmp_path / "state" / "managed_skins.json",
        tmp_path / "cache",
        ltk_app_data_dir=tmp_path / "ltk-data",
        report_dir=tmp_path / "reports",
        history_checkpoint_records=2,
    )
    real_atomic_write = real_atomic_write_bytes
    write_calls = 0

    def fail_second_checkpoint(path: Path, value: bytes) -> None:
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise OSError("checkpoint disk failure")
        real_atomic_write(path, value)

    monkeypatch.setattr(migration_module, "atomic_write_bytes", fail_second_checkpoint)

    with pytest.raises(MigrationHistoryError, match="persist"):
        service.migrate(installed)

    assert write_calls == 2
    assert len(queued_archives(tmp_path / "ltk-data" / "archives")) == 2
    durable = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(durable["packages"]) == 2
    assert not list((tmp_path / "ltk-data" / "archives").glob("*.partial"))

    monkeypatch.setattr(migration_module, "atomic_write_bytes", real_atomic_write)
    resumed = service.migrate(installed)
    assert resumed.skipped == 2
    assert resumed.queued == 3
    final = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(final["packages"]) == 5


def test_manager_start_after_first_queue_flushes_blocked_partial_history(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "first", wad=b"first")
    create_live_mod(installed, "second", wad=b"second")
    ltk_running = False

    def start_ltk_after_first(event: MigrationProgress) -> None:
        nonlocal ltk_running
        if event.phase == "migrating" and event.completed == 1:
            ltk_running = True

    service = make_service(tmp_path, ltk_is_running=lambda: ltk_running)
    partial = service.migrate(installed, progress=start_ltk_after_first)

    assert partial.blocked
    assert partial.queued == 1
    state = json.loads(service.migration_state_path.read_text(encoding="utf-8"))
    assert len(state["packages"]) == 1


def test_manager_starting_during_work_returns_blocked_partial_result(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom")
    ltk_running = False

    def report_progress(event: MigrationProgress) -> None:
        nonlocal ltk_running
        if event.phase == "migrating":
            ltk_running = True

    service = make_service(tmp_path, ltk_is_running=lambda: ltk_running)

    result = service.migrate(installed, progress=report_progress)

    assert result.blocked
    assert result.queued == 0
    assert any("Close LTK Manager" in issue.reason for issue in result.issues)
    assert result.report_path.is_file()


def test_malformed_and_over_limit_mods_are_reported_without_touching_source(
    tmp_path: Path,
) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    malformed = installed / "missing-wad"
    (malformed / "META").mkdir(parents=True)
    metadata = malformed / "META" / "info.json"
    metadata.write_text("{}", encoding="utf-8")
    original = metadata.read_bytes()
    service = make_service(tmp_path, max_members_per_mod=1)

    result = service.migrate(installed)

    assert result.status == "completed"
    assert result.failed == 1
    assert result.queued == 0
    assert result.issues
    assert metadata.read_bytes() == original
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["failed"] == 1
    assert report["issues"][0]["source"].endswith("missing-wad")


def test_pre_cancelled_migration_is_reported_without_archives(tmp_path: Path) -> None:
    installed = tmp_path / "installed"
    installed.mkdir()
    create_live_mod(installed, "custom")
    cancelled = threading.Event()
    cancelled.set()
    service = make_service(tmp_path)

    result = service.migrate(installed, cancel_event=cancelled)

    assert result.cancelled
    assert not result.archives_dir.exists()
    assert result.report_path.is_file()
