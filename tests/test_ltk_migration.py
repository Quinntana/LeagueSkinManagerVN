from __future__ import annotations

import json
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.ltk_migration import (
    MANAGED_PACKAGE_PREFIX,
    LtkMigrationService,
    LtkReconcileError,
    ReconcileBlockedError,
    ReconcileBusyError,
    ReconcileProgress,
)
from league_skin_manager.skin_installer import git_blob_sha, managed_directory_name


def create_fantome(path: Path, *, name: str, wad: bytes) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META/info.json", json.dumps({"Name": name}))
        archive.writestr("WAD/Test.wad.client", wad)
    return path


class Workspace:
    """A managed skin set plus an LTK storage directory, both on disk."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        self.state_path = tmp_path / "state" / "managed_skins.json"
        self.cache_dir = tmp_path / "cache" / "packages"
        self.storage_dir = tmp_path / "ltk-data"
        self.archives_dir = self.storage_dir / "archives"
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.entries: list[dict[str, Any]] = []
        self.removed: list[tuple[str, ...]] = []
        self.toggle_resets = 0

    def add_skin(self, champion: str, name: str, *, wad: bytes | None = None) -> str:
        """Add one managed skin together with its verified cache package."""

        source_path = f"skins/{champion}/{name}.fantome"
        staged = self.root / f"{champion}-{name}.fantome".replace(" ", "-")
        create_fantome(staged, name=name, wad=wad if wad is not None else name.encode())
        source_sha = git_blob_sha(staged)
        package = self.cache_dir / f"{source_sha}.fantome"
        package.write_bytes(staged.read_bytes())
        staged.unlink()
        self.entries.append(
            {
                "champion": champion,
                "name": name,
                "source_path": source_path,
                "source_sha": source_sha,
                "size": package.stat().st_size,
                "directory": managed_directory_name(champion, name, source_path),
                "content_sha256": "a" * 64,
            }
        )
        self.write_state()
        return source_sha

    def drop_skin(self, name: str) -> None:
        self.entries = [entry for entry in self.entries if entry["name"] != name]
        self.write_state()

    def write_state(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "transaction_id": "test",
                    "source_commit": "a" * 40,
                    "patch": "16.14.1",
                    "entries": self.entries,
                }
            ),
            encoding="utf-8",
        )

    def service(self, **options: Any) -> LtkMigrationService:
        options.setdefault("remove_ltk_mods", self._remove_mods)
        options.setdefault("clear_ltk_toggles", self._clear_toggles)
        return LtkMigrationService(
            self.state_path,
            self.cache_dir,
            ltk_app_data_dir=self.storage_dir,
            report_dir=self.root / "reports",
            **options,
        )

    def _remove_mods(self, mod_ids: tuple[str, ...]) -> None:
        """Mirror LtkSkinCleanupService.remove_mods: delete the stored packages."""

        self.removed.append(mod_ids)
        for mod_id in mod_ids:
            (self.archives_dir / f"{mod_id}.fantome").unlink(missing_ok=True)

    def _clear_toggles(self) -> int:
        self.toggle_resets += 1
        return 0

    def queued(self) -> list[str]:
        if not self.archives_dir.is_dir():
            return []
        return sorted(path.name for path in self.archives_dir.glob("*.fantome"))

    def simulate_ltk_import(self, mod_id: str) -> Path:
        """Reproduce LTK's import: rename to <mod-id>.fantome, bytes unchanged."""

        queued = next(self.archives_dir.glob(f"{MANAGED_PACKAGE_PREFIX}*.fantome"))
        adopted = self.archives_dir / f"{mod_id}.fantome"
        queued.rename(adopted)
        return adopted


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace(tmp_path)


def test_reconcile_reaches_the_baseline_in_one_pass(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.add_skin("Aatrox", "Mecha Aatrox")
    service = workspace.service()

    result = service.reconcile()

    assert result.status == "completed"
    assert (result.expected, result.added, result.removed) == (2, 2, 0)
    assert len(workspace.queued()) == 2
    assert all(name.startswith(MANAGED_PACKAGE_PREFIX) for name in workspace.queued())
    assert workspace.toggle_resets == 1


def test_reconcile_is_idempotent(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service()
    first = service.reconcile()
    before = workspace.queued()

    second = service.reconcile()

    assert (second.added, second.removed) == (0, 0)
    assert second.unchanged == first.expected
    assert workspace.queued() == before


def test_reconcile_is_idempotent_after_ltk_imports_the_package(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service()
    assert service.reconcile().added == 1
    adopted = workspace.simulate_ltk_import("11111111-2222-3333-4444-555555555555")

    result = service.reconcile()

    assert (result.added, result.removed) == (0, 0)
    assert result.unchanged == 1
    assert adopted.exists()
    assert workspace.removed == []


def test_reconcile_removes_a_superseded_version(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri", wad=b"old-patch")
    service = workspace.service()
    assert service.reconcile().added == 1
    workspace.simulate_ltk_import("old-mod-id")

    workspace.drop_skin("Foxfire Ahri")
    workspace.add_skin("Ahri", "Foxfire Ahri", wad=b"new-patch")
    result = service.reconcile()

    assert (result.added, result.removed) == (1, 1)
    assert workspace.removed == [("old-mod-id",)]


def test_reconcile_removes_a_skin_that_left_the_managed_set(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.add_skin("Aatrox", "Mecha Aatrox")
    service = workspace.service()
    assert service.reconcile().added == 2

    workspace.drop_skin("Mecha Aatrox")
    result = service.reconcile()

    assert (result.expected, result.added, result.removed) == (1, 0, 1)
    assert len(workspace.queued()) == 1


def test_reconcile_removes_packages_the_user_imported(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service()
    assert service.reconcile().added == 1
    workspace.simulate_ltk_import("vn-owned-id")
    create_fantome(
        workspace.archives_dir / "user-imported-id.fantome",
        name="Someone else's skin",
        wad=b"foreign",
    )

    result = service.reconcile()

    assert result.removed == 1
    assert workspace.removed == [("user-imported-id",)]


def test_reconcile_against_a_prepopulated_library_replaces_it_exactly(
    workspace: Workspace,
) -> None:
    """A library built by another tool is corrected, not duplicated."""

    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.add_skin("Aatrox", "Mecha Aatrox")
    workspace.archives_dir.mkdir(parents=True)
    for index in range(3):
        create_fantome(
            workspace.archives_dir / f"pre-existing-{index}.fantome",
            name=f"Legacy skin {index}",
            wad=f"legacy-{index}".encode(),
        )
    service = workspace.service()

    result = service.reconcile()

    assert (result.expected, result.added, result.removed) == (2, 2, 3)
    assert len(workspace.queued()) == 2
    assert workspace.removed == [("pre-existing-0", "pre-existing-1", "pre-existing-2")]


def test_reconcile_skips_a_skin_with_no_cached_package(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.add_skin("Aatrox", "Mecha Aatrox")
    next(iter(workspace.cache_dir.iterdir())).unlink()
    service = workspace.service()

    result = service.reconcile()

    assert (result.expected, result.added) == (1, 1)


def test_reconcile_is_blocked_while_a_manager_runs(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service(ltk_is_running=lambda: True)

    with pytest.raises(ReconcileBlockedError, match="Close LTK Manager"):
        service.reconcile()

    assert workspace.queued() == []
    assert workspace.toggle_resets == 0


def test_reconcile_is_blocked_when_process_state_is_unknown(workspace: Workspace) -> None:
    def explode() -> bool:
        raise OSError("process table unavailable")

    service = workspace.service(cslol_is_running=explode)

    with pytest.raises(ReconcileBlockedError, match="Could not verify"):
        service.reconcile()


def test_pre_cancelled_reconcile_changes_nothing(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    cancelled = threading.Event()
    cancelled.set()
    service = workspace.service()

    result = service.reconcile(cancel_event=cancelled)

    assert result.cancelled
    assert workspace.queued() == []
    assert result.report_path.is_file()


def test_reconcile_reports_progress_and_writes_a_report(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service()
    seen: list[ReconcileProgress] = []

    result = service.reconcile(progress=seen.append)

    assert any(item.phase == "queueing" for item in seen)
    assert any(item.skin_name == "Foxfire Ahri" for item in seen)
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["status"] == "completed"
    assert (report["added"], report["expected"]) == (1, 1)


def test_reconcile_rejects_a_non_callable_progress(workspace: Workspace) -> None:
    service = workspace.service()

    with pytest.raises(TypeError, match="progress must be callable"):
        service.reconcile(progress=object())  # type: ignore[arg-type]


def test_reconcile_and_inspection_are_exclusive(workspace: Workspace) -> None:
    service = workspace.service()
    assert service._lock.acquire(blocking=False)
    try:
        with pytest.raises(ReconcileBusyError, match="already in progress"):
            service.reconcile()
        with pytest.raises(ReconcileBusyError, match="already in progress"):
            service.inspect_baseline()
    finally:
        service._lock.release()


def test_inspect_baseline_reports_drift_without_changing_anything(
    workspace: Workspace,
) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.add_skin("Aatrox", "Mecha Aatrox")
    service = workspace.service()

    empty = service.inspect_baseline()

    assert (empty.expected, empty.present, empty.extra) == (2, 0, 0)
    assert empty.missing == 2
    assert not empty.at_baseline
    assert workspace.queued() == []

    service.reconcile()
    current = service.inspect_baseline()

    assert (current.expected, current.present, current.extra) == (2, 2, 0)
    assert current.at_baseline


def test_inspect_baseline_counts_foreign_packages_as_extra(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.archives_dir.mkdir(parents=True)
    create_fantome(workspace.archives_dir / "foreign.fantome", name="Foreign", wad=b"foreign")
    service = workspace.service()

    status = service.inspect_baseline()

    assert (status.expected, status.present, status.extra) == (1, 0, 1)
    assert not status.at_baseline


def test_reconcile_without_a_removal_boundary_reports_instead_of_deleting(
    workspace: Workspace,
) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    workspace.archives_dir.mkdir(parents=True)
    foreign = create_fantome(
        workspace.archives_dir / "foreign-id.fantome",
        name="Foreign",
        wad=b"foreign",
    )
    service = workspace.service(remove_ltk_mods=None)

    result = service.reconcile()

    assert result.removed == 0
    assert foreign.exists()
    assert any("no removal boundary" in issue.reason for issue in result.issues)


def test_a_corrupt_package_index_degrades_to_a_fresh_digest(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service()
    service.reconcile()

    assert service.package_index_path.is_file()
    index = json.loads(service.package_index_path.read_text(encoding="utf-8"))
    assert index["schema_version"] == 1
    assert len(index["packages"]) == 1

    service.package_index_path.write_text("not json", encoding="utf-8")
    repeat = service.reconcile()

    assert (repeat.added, repeat.removed) == (0, 0)


def test_indexes_may_not_live_inside_ltk_storage(workspace: Workspace) -> None:
    workspace.add_skin("Ahri", "Foxfire Ahri")
    service = workspace.service(
        archive_index_path=workspace.storage_dir / "archive-index.json",
    )

    with pytest.raises(LtkReconcileError, match="cannot be stored in LTK-owned data"):
        service.reconcile()


def test_storage_dir_honours_an_absolute_mod_storage_path(workspace: Workspace) -> None:
    workspace.storage_dir.mkdir(parents=True, exist_ok=True)
    custom = workspace.root / "custom-storage"
    (workspace.storage_dir / "settings.json").write_text(
        json.dumps({"modStoragePath": str(custom)}),
        encoding="utf-8",
    )
    service = workspace.service()

    assert service.resolve_storage_dir() == custom


@pytest.mark.parametrize(
    "settings",
    ["not json", json.dumps({"modStoragePath": "relative/path"}), json.dumps([1, 2])],
)
def test_storage_dir_falls_back_for_unusable_settings(
    workspace: Workspace,
    settings: str,
) -> None:
    workspace.storage_dir.mkdir(parents=True, exist_ok=True)
    (workspace.storage_dir / "settings.json").write_text(settings, encoding="utf-8")
    service = workspace.service()

    assert service.resolve_storage_dir() == workspace.storage_dir


def test_resource_limits_must_be_positive(workspace: Workspace) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        workspace.service(max_mods=0)


def test_separate_index_files_are_required(workspace: Workspace) -> None:
    shared = workspace.root / "shared-index.json"
    with pytest.raises(ValueError, match="must be separate"):
        workspace.service(archive_index_path=shared, package_index_path=shared)
