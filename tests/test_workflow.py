from __future__ import annotations

import logging
from pathlib import Path
from threading import Event

import pytest

from league_skin_manager.controller import AppState
from league_skin_manager.manager_update import ManagerUpdateStatus, UntrustedReleaseError
from league_skin_manager.skin_source import ManifestFetchError, SkinManifest
from league_skin_manager.sync_service import SyncResult
from league_skin_manager.workflow import SynchronizationWorkflow


class Source:
    def __init__(self, manifest: SkinManifest | Exception) -> None:
        self.manifest = manifest

    def fetch_manifest(self) -> SkinManifest:
        if isinstance(self.manifest, Exception):
            raise self.manifest
        return self.manifest

    def download(self, *_args: object, **_kwargs: object) -> Path:
        raise AssertionError("not used")


class Syncer:
    def __init__(self, result: SyncResult) -> None:
        self.result = result
        self.calls = 0

    def sync(self, *_args: object, **_kwargs: object) -> SyncResult:
        self.calls += 1
        return self.result


class Updater:
    def __init__(self, result: ManagerUpdateStatus | Exception) -> None:
        self.result = result

    def update(self, _cancel: Event) -> ManagerUpdateStatus:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def workflow(
    tmp_path: Path,
    source: Source,
    syncer: Syncer,
    updater: Updater,
) -> SynchronizationWorkflow:
    return SynchronizationWorkflow(
        source=source,  # type: ignore[arg-type]
        sync_service=syncer,  # type: ignore[arg-type]
        manager_updater=updater,
        manager_executable=tmp_path / "manager" / "cslol-manager.exe",
        installed_dir=tmp_path / "manager" / "installed",
        logger=logging.getLogger("test"),
    )


def test_success_reports_patch_and_deferred_manager_update(tmp_path: Path) -> None:
    manifest = SkinManifest("a" * 40, "16.13.1", ())
    syncer = Syncer(SyncResult("a" * 40, "16.13.1", 1920, 20, 1900, 0))
    value = workflow(
        tmp_path,
        Source(manifest),
        syncer,
        Updater(ManagerUpdateStatus.DEFERRED_RUNNING),
    )(Event())
    assert value.state is AppState.READY
    assert value.detail == "Ready - 1920 skins (16.13.1); manager update deferred"
    assert syncer.calls == 1


def test_offline_source_uses_existing_manager_and_mods(tmp_path: Path) -> None:
    manager = tmp_path / "manager"
    installed = manager / "installed" / "custom-mod"
    installed.mkdir(parents=True)
    (manager / "cslol-manager.exe").write_bytes(b"manager")
    value = workflow(
        tmp_path,
        Source(ManifestFetchError("offline")),
        Syncer(SyncResult("", None, 0, 0, 0, 0)),
        Updater(OSError("offline")),
    )(Event())
    assert value.state is AppState.OFFLINE_READY
    assert value.detail == "Offline - using installed skins"


def test_first_run_offline_remains_an_error(tmp_path: Path) -> None:
    operation = workflow(
        tmp_path,
        Source(ManifestFetchError("offline")),
        Syncer(SyncResult("", None, 0, 0, 0, 0)),
        Updater(OSError("offline")),
    )
    with pytest.raises(ManifestFetchError, match="offline"):
        operation(Event())


def test_untrusted_manager_release_is_actionable_without_discarding_skin_sync(
    tmp_path: Path,
) -> None:
    manifest = SkinManifest("a" * 40, "16.13.1", ())
    syncer = Syncer(SyncResult("a" * 40, "16.13.1", 1920, 20, 1900, 0))
    value = workflow(
        tmp_path,
        Source(manifest),
        syncer,
        Updater(UntrustedReleaseError("future release is not reviewed")),
    )(Event())

    assert value.state is AppState.OFFLINE_READY
    assert value.detail == ("Skins ready (16.13.1); install CSLOL Manager manually - see log")
    assert syncer.calls == 1
