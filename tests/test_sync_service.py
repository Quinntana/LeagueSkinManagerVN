from __future__ import annotations

import json
import shutil
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from league_skin_manager import sync_service as sync_module
from league_skin_manager.skin_installer import (
    git_blob_sha,
    managed_directory_name,
    validate_fantome,
)
from league_skin_manager.skin_source import DownloadCancelledError, SkinAsset, SkinManifest
from league_skin_manager.sync_service import (
    ManagedCollisionError,
    ManagedStateError,
    SkinSyncService,
    SyncCancelled,
    SyncError,
    SyncMutationBlocked,
    SyncProgress,
    TransactionError,
)


def create_fantome(path: Path, marker: str) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META/info.json", json.dumps({"Name": marker}))
        archive.writestr("WAD/Test.wad.client", marker.encode())
    return path


def make_asset(archive: Path, *, champion: str, name: str) -> SkinAsset:
    return SkinAsset(
        champion=champion,
        name=name,
        path=f"skins/{champion}/{name}.fantome",
        size=archive.stat().st_size,
        sha=git_blob_sha(archive),
    )


def make_manifest(*assets: SkinAsset, commit: str = "a" * 40) -> SkinManifest:
    return SkinManifest(commit=commit, patch="16.13.1", assets=tuple(assets))


class FakeSource:
    def __init__(self, files: dict[str, Path]) -> None:
        self.files = files
        self.calls: list[str] = []
        self.error: BaseException | None = None
        self.on_download: Callable[[], None] | None = None

    def download(
        self,
        asset: SkinAsset,
        target: Path,
        *,
        cancel_event: object | None = None,
        progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        self.calls.append(asset.path)
        if self.on_download is not None:
            self.on_download()
        if self.error is not None:
            raise self.error
        source = self.files[asset.path]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if progress is not None:
            progress(target.stat().st_size, target.stat().st_size)
        return target


def make_service(tmp_path: Path, *, workers: int = 2, **options: Any) -> SkinSyncService:
    return SkinSyncService(
        tmp_path / "cslol-manager" / "installed",
        tmp_path / "state" / "managed-skins.json",
        cache_dir=tmp_path / "cache",
        max_workers=workers,
        **options,
    )


def test_initial_sync_preserves_user_mods_and_profiles(tmp_path: Path) -> None:
    ahri_archive = create_fantome(tmp_path / "ahri.fantome", "Ahri-v1")
    lux_archive = create_fantome(tmp_path / "lux.fantome", "Lux-v1")
    ahri = make_asset(ahri_archive, champion="Ahri", name="Foxfire Ahri")
    lux = make_asset(lux_archive, champion="Lux", name="Elementalist Lux")
    source = FakeSource({ahri.path: ahri_archive, lux.path: lux_archive})
    service = make_service(tmp_path)
    user_mod = service.installed_dir / "My hand-made mod"
    user_mod.mkdir(parents=True)
    (user_mod / "keep.txt").write_text("mine", encoding="utf-8")
    profiles = service.installed_dir.parent / "profiles"
    profiles.mkdir()
    (profiles / "ranked.profile").write_text("profile", encoding="utf-8")
    events: list[SyncProgress] = []

    result = service.sync(source, make_manifest(ahri, lux), progress=events.append)

    assert result.installed == 2
    assert result.downloaded == 2
    assert (user_mod / "keep.txt").read_text(encoding="utf-8") == "mine"
    assert (profiles / "ranked.profile").read_text(encoding="utf-8") == "profile"
    state = service.load_state()
    assert {entry.source_path for entry in state.entries} == {ahri.path, lux.path}
    for entry in state.entries:
        assert (service.installed_dir / entry.directory / "WAD" / "Test.wad.client").is_file()
    assert events[-1].phase == "complete"


def test_identical_manifest_is_noop_and_reuses_live_install(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "v1")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    manifest = make_manifest(asset)
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    service.sync(source, manifest)
    original_state = service.state_path.read_bytes()
    source.calls.clear()

    result = service.sync(source, manifest)

    assert result.downloaded == 0
    assert result.reused == 1
    assert source.calls == []
    assert service.state_path.read_bytes() == original_state


def test_corrupt_live_mod_is_rebuilt_from_verified_cache(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "v1")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    manifest = make_manifest(asset)
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    service.sync(source, manifest)
    entry = service.load_state().entries[0]
    shutil.rmtree(service.installed_dir / entry.directory / "WAD")
    source.calls.clear()

    result = service.sync(source, manifest)

    assert result.downloaded == 0
    assert source.calls == []
    assert (service.installed_dir / entry.directory / "WAD" / "Test.wad.client").is_file()


def test_same_shape_live_tampering_is_rebuilt_from_verified_cache(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "trusted")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    manifest = make_manifest(asset)
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    service.sync(source, manifest)
    entry = service.load_state().entries[0]
    assert len(entry.content_sha256) == 64
    live_wad = service.installed_dir / entry.directory / "WAD" / "Test.wad.client"
    live_wad.write_bytes(b"tampered")
    source.calls.clear()

    result = service.sync(source, manifest)

    assert result.downloaded == 0
    assert result.reused == 0
    assert source.calls == []
    assert live_wad.read_bytes() == b"trusted"


def test_legacy_state_without_content_digest_is_never_reused(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "trusted")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    manifest = make_manifest(asset)
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    service.sync(source, manifest)
    raw_state = json.loads(service.state_path.read_text(encoding="utf-8"))
    del raw_state["entries"][0]["content_sha256"]
    service.state_path.write_text(json.dumps(raw_state), encoding="utf-8")
    entry = service.load_state().entries[0]
    live_wad = service.installed_dir / entry.directory / "WAD" / "Test.wad.client"
    live_wad.write_bytes(b"tampered")
    source.calls.clear()

    result = service.sync(source, manifest)

    assert result.reused == 0
    assert result.downloaded == 0
    assert source.calls == []
    assert live_wad.read_bytes() == b"trusted"


def test_failed_update_never_changes_live_mods_or_state(tmp_path: Path) -> None:
    old_archive = create_fantome(tmp_path / "old.fantome", "old")
    old = make_asset(old_archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({old.path: old_archive})
    service = make_service(tmp_path)
    service.sync(source, make_manifest(old))
    state_before = service.state_path.read_bytes()
    entry = service.load_state().entries[0]
    live_wad = service.installed_dir / entry.directory / "WAD" / "Test.wad.client"

    new_archive = create_fantome(tmp_path / "new.fantome", "new")
    new = make_asset(new_archive, champion="Ahri", name="Foxfire Ahri")
    source.files[new.path] = new_archive
    source.error = OSError("network down")

    with pytest.raises(OSError, match="network down"):
        service.sync(source, make_manifest(new, commit="b" * 40))

    assert live_wad.read_bytes() == b"old"
    assert service.state_path.read_bytes() == state_before
    assert not service.journal_path.exists()


def test_update_removes_only_prior_managed_directories(tmp_path: Path) -> None:
    ahri_archive = create_fantome(tmp_path / "ahri.fantome", "ahri")
    lux_archive = create_fantome(tmp_path / "lux.fantome", "lux")
    ahri = make_asset(ahri_archive, champion="Ahri", name="Foxfire Ahri")
    lux = make_asset(lux_archive, champion="Lux", name="Elementalist Lux")
    source = FakeSource({ahri.path: ahri_archive, lux.path: lux_archive})
    service = make_service(tmp_path)
    service.sync(source, make_manifest(ahri, lux))
    lux_directory = managed_directory_name(lux.champion, lux.name, lux.path)
    unknown = service.installed_dir / "untracked-custom-mod"
    unknown.mkdir()
    (unknown / "keep").write_text("yes", encoding="utf-8")

    result = service.sync(source, make_manifest(ahri, commit="c" * 40))

    assert result.removed == 1
    assert not (service.installed_dir / lux_directory).exists()
    assert (unknown / "keep").read_text(encoding="utf-8") == "yes"


def test_unowned_managed_name_collision_is_never_overwritten(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "downloaded")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    collision = service.installed_dir / managed_directory_name(
        asset.champion,
        asset.name,
        asset.path,
    )
    collision.mkdir(parents=True)
    (collision / "user.txt").write_text("do not replace", encoding="utf-8")

    with pytest.raises(ManagedCollisionError):
        service.sync(source, make_manifest(asset))

    assert (collision / "user.txt").read_text(encoding="utf-8") == "do not replace"
    assert not service.state_path.exists()


def test_state_write_failure_rolls_live_directories_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_archive = create_fantome(tmp_path / "old.fantome", "old")
    old = make_asset(old_archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({old.path: old_archive})
    service = make_service(tmp_path)
    service.sync(source, make_manifest(old))
    state_before = service.state_path.read_bytes()
    live_directory = service.installed_dir / service.load_state().entries[0].directory

    new_archive = create_fantome(tmp_path / "new.fantome", "new")
    new = make_asset(new_archive, champion="Ahri", name="Foxfire Ahri")
    source.files[new.path] = new_archive
    real_atomic_write = sync_module.atomic_write_json

    def fail_state_write(path: Path, value: object) -> None:
        if path == service.state_path:
            raise OSError("disk full")
        real_atomic_write(path, value)

    monkeypatch.setattr(sync_module, "atomic_write_json", fail_state_write)

    with pytest.raises(TransactionError, match="restored"):
        service.sync(source, make_manifest(new, commit="d" * 40))

    assert (live_directory / "WAD" / "Test.wad.client").read_bytes() == b"old"
    assert service.state_path.read_bytes() == state_before
    assert not service.journal_path.exists()


def test_next_start_recovers_an_interrupted_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_archive = create_fantome(tmp_path / "old.fantome", "old")
    old = make_asset(old_archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({old.path: old_archive})
    service = make_service(tmp_path)
    service.sync(source, make_manifest(old))
    state_before = service.state_path.read_bytes()
    live_directory = service.installed_dir / service.load_state().entries[0].directory

    new_archive = create_fantome(tmp_path / "new.fantome", "new")
    new = make_asset(new_archive, champion="Ahri", name="Foxfire Ahri")
    source.files[new.path] = new_archive
    real_atomic_write = sync_module.atomic_write_json

    def fail_state_write(path: Path, value: object) -> None:
        if path == service.state_path:
            raise OSError("simulated process failure")
        real_atomic_write(path, value)

    def interrupted_rollback(_journal: object) -> None:
        raise RuntimeError("process stopped before rollback")

    monkeypatch.setattr(sync_module, "atomic_write_json", fail_state_write)
    monkeypatch.setattr(service, "_rollback", interrupted_rollback)

    with pytest.raises(TransactionError, match="recovery journal was preserved"):
        service.sync(source, make_manifest(new, commit="f" * 40))

    assert service.journal_path.exists()
    assert (live_directory / "WAD" / "Test.wad.client").read_bytes() == b"new"
    assert service.state_path.read_bytes() == state_before

    monkeypatch.undo()
    blocked = make_service(tmp_path, manager_is_running=lambda: True)
    with pytest.raises(SyncMutationBlocked, match="running"):
        blocked.recover()
    assert service.journal_path.exists()
    assert (live_directory / "WAD" / "Test.wad.client").read_bytes() == b"new"

    restarted = make_service(tmp_path)
    assert restarted.recover()
    assert (live_directory / "WAD" / "Test.wad.client").read_bytes() == b"old"
    assert restarted.state_path.read_bytes() == state_before
    assert not restarted.journal_path.exists()


def test_committed_transaction_is_not_rolled_back_when_cleanup_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "committed")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    real_rmtree = sync_module.shutil.rmtree

    def fail_transaction_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        candidate = Path(path)
        if candidate.name.startswith(sync_module.TRANSACTION_DIRECTORY_PREFIX):
            raise OSError("directory temporarily busy")
        real_rmtree(candidate, *args, **kwargs)

    monkeypatch.setattr(sync_module.shutil, "rmtree", fail_transaction_cleanup)
    result = service.sync(source, make_manifest(asset))
    entry = service.load_state().entries[0]
    live_wad = service.installed_dir / entry.directory / "WAD" / "Test.wad.client"

    assert result.installed == 1
    assert service.journal_path.exists()
    assert live_wad.read_bytes() == b"committed"

    monkeypatch.undo()
    restarted = make_service(tmp_path)
    assert restarted.recover()
    assert live_wad.read_bytes() == b"committed"
    assert not restarted.journal_path.exists()


def test_cancellation_before_commit_preserves_live_state(tmp_path: Path) -> None:
    old_archive = create_fantome(tmp_path / "old.fantome", "old")
    old = make_asset(old_archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({old.path: old_archive})
    service = make_service(tmp_path)
    service.sync(source, make_manifest(old))
    state_before = service.state_path.read_bytes()

    new_archive = create_fantome(tmp_path / "new.fantome", "new")
    new = make_asset(new_archive, champion="Ahri", name="Foxfire Ahri")
    source.files[new.path] = new_archive
    cancelled = threading.Event()

    def cancel_download() -> None:
        cancelled.set()

    source.on_download = cancel_download

    with pytest.raises(SyncCancelled):
        service.sync(
            source,
            make_manifest(new, commit="e" * 40),
            cancel_event=cancelled,
        )

    assert service.state_path.read_bytes() == state_before


def test_manager_starting_during_staging_blocks_live_commit(tmp_path: Path) -> None:
    old_archive = create_fantome(tmp_path / "old.fantome", "old")
    old = make_asset(old_archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({old.path: old_archive})
    running = False
    service = make_service(tmp_path, manager_is_running=lambda: running)
    service.sync(source, make_manifest(old))
    state_before = service.state_path.read_bytes()
    live_directory = service.installed_dir / service.load_state().entries[0].directory

    new_archive = create_fantome(tmp_path / "new.fantome", "new")
    new = make_asset(new_archive, champion="Ahri", name="Foxfire Ahri")
    source.files[new.path] = new_archive

    def start_manager() -> None:
        nonlocal running
        running = True

    source.on_download = start_manager
    with pytest.raises(SyncMutationBlocked, match="running"):
        service.sync(source, make_manifest(new, commit="e" * 40))

    assert service.state_path.read_bytes() == state_before
    assert (live_directory / "WAD" / "Test.wad.client").read_bytes() == b"old"
    assert not service.journal_path.exists()


def test_manager_lookup_failure_blocks_before_creating_sync_directories(
    tmp_path: Path,
) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "v1")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")

    def fail_lookup() -> bool:
        raise OSError("snapshot unavailable")

    service = make_service(tmp_path, manager_is_running=fail_lookup)

    with pytest.raises(SyncMutationBlocked, match="verify"):
        service.sync(FakeSource({asset.path: archive}), make_manifest(asset))

    assert not service.installed_dir.exists()
    assert not service.state_path.exists()


def test_source_cancellation_is_normalized(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "v1")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({asset.path: archive})
    source.error = DownloadCancelledError("cancelled")
    service = make_service(tmp_path)

    with pytest.raises(SyncCancelled):
        service.sync(source, make_manifest(asset))

    assert not service.state_path.exists()
    assert list(service.installed_dir.iterdir()) == []


def test_corrupt_state_fails_closed_before_download(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "v1")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({asset.path: archive})
    service = make_service(tmp_path)
    service.state_path.parent.mkdir(parents=True)
    service.state_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ManagedStateError, match="refusing"):
        service.sync(source, make_manifest(asset))

    assert source.calls == []


def test_manifest_compressed_limits_fail_before_download(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "large")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    source = FakeSource({asset.path: archive})

    with pytest.raises(SyncError, match="invalid artifact metadata"):
        make_service(
            tmp_path,
            max_asset_compressed_bytes=asset.size - 1,
            free_space_reserve_bytes=0,
        ).sync(source, make_manifest(asset))
    assert source.calls == []

    with pytest.raises(SyncError, match="aggregate compressed"):
        make_service(
            tmp_path,
            max_total_compressed_bytes=asset.size - 1,
            free_space_reserve_bytes=0,
        ).sync(source, make_manifest(asset))
    assert source.calls == []


def test_aggregate_uncompressed_limit_fails_before_extraction(tmp_path: Path) -> None:
    first_archive = create_fantome(tmp_path / "first.fantome", "first")
    second_archive = create_fantome(tmp_path / "second.fantome", "second")
    first = make_asset(first_archive, champion="Ahri", name="First")
    second = make_asset(second_archive, champion="Lux", name="Second")
    expanded = (
        validate_fantome(first_archive).uncompressed_bytes
        + validate_fantome(second_archive).uncompressed_bytes
    )
    source = FakeSource({first.path: first_archive, second.path: second_archive})
    service = make_service(
        tmp_path,
        max_total_uncompressed_bytes=expanded - 1,
        free_space_reserve_bytes=0,
    )

    with pytest.raises(SyncError, match="aggregate uncompressed"):
        service.sync(source, make_manifest(first, second))

    assert not service.state_path.exists()
    assert list(service.installed_dir.iterdir()) == []


def test_free_space_preflight_runs_before_download_and_staging(tmp_path: Path) -> None:
    archive = create_fantome(tmp_path / "skin.fantome", "trusted")
    asset = make_asset(archive, champion="Ahri", name="Foxfire Ahri")
    manifest = make_manifest(asset)
    source = FakeSource({asset.path: archive})

    def no_space(_path: Path) -> SimpleNamespace:
        return SimpleNamespace(free=0)

    service = make_service(
        tmp_path,
        free_space_reserve_bytes=0,
        disk_usage=no_space,
    )

    with pytest.raises(SyncError, match="skin downloads"):
        service.sync(source, manifest)
    assert source.calls == []

    normal = make_service(tmp_path, free_space_reserve_bytes=0)
    normal.sync(source, manifest)
    entry = normal.load_state().entries[0]
    live_wad = normal.installed_dir / entry.directory / "WAD" / "Test.wad.client"
    live_wad.write_bytes(b"tampered")
    source.calls.clear()
    staging_limited = make_service(
        tmp_path,
        free_space_reserve_bytes=0,
        disk_usage=no_space,
    )

    with pytest.raises(SyncError, match="transaction staging"):
        staging_limited.sync(source, manifest)
    assert source.calls == []
    assert live_wad.read_bytes() == b"tampered"
