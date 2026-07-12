from __future__ import annotations

import hashlib
import io
import logging
import shutil
import stat
import zipfile
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from league_skin_manager import manager_update as update_module
from league_skin_manager.manager_update import (
    TRUSTED_RELEASE_ASSETS,
    ManagerRelease,
    ManagerReleaseClient,
    ManagerTransactionError,
    ManagerUpdater,
    ManagerUpdateStatus,
    ReleaseAsset,
    UntrustedReleaseError,
    _safe_extract,
)


class Response:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.closed = False

    def raise_for_status(self) -> None:
        return

    def json(self) -> Any:
        return self.payload

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.closed = False

    def get(self, _url: str, **_kwargs: Any) -> Response:
        return Response(self.payload)

    def close(self) -> None:
        self.closed = True


def test_release_client_prefers_zip_and_validates_metadata() -> None:
    payload = {
        "tag_name": "2026.1",
        "assets": [
            {
                "name": "cslol-manager-windows.exe",
                "browser_download_url": "https://github.com/example/manager.exe",
                "size": 20,
            },
            {
                "name": "manager.zip",
                "browser_download_url": "https://github.com/example/manager.zip",
                "size": 10,
            },
        ],
    }
    client = ManagerReleaseClient(
        "https://api.github.test/latest",
        logging.getLogger("test"),
        Session(payload),  # type: ignore[arg-type]
    )

    release = client.latest()

    assert release.version == "2026.1"
    assert release.asset.name == "manager.zip"


def test_release_client_closes_only_a_session_it_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned = Session({})
    monkeypatch.setattr(update_module.requests, "Session", lambda: owned)
    client = ManagerReleaseClient("https://api.github.test/latest", logging.getLogger("test"))
    client.close()
    assert owned.closed

    injected = Session({})
    client = ManagerReleaseClient(
        "https://api.github.test/latest",
        logging.getLogger("test"),
        injected,  # type: ignore[arg-type]
    )
    client.close()
    assert not injected.closed


def test_safe_extract_rejects_archive_traversal(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../escape.exe", b"bad")
    stream.seek(0)

    with zipfile.ZipFile(stream) as archive, pytest.raises(ValueError, match="Unsafe"):
        _safe_extract(archive, tmp_path)

    assert not (tmp_path.parent / "escape.exe").exists()


class FakeReleaseClient:
    def __init__(
        self,
        archive: Path,
        *,
        version: str = "2026.1",
        asset_name: str = "manager.zip",
        reported_size: int | None = None,
    ) -> None:
        self.archive = archive
        self.version = version
        self.asset_name = asset_name
        self.reported_size = reported_size
        self.downloads = 0

    def latest(self) -> ManagerRelease:
        return ManagerRelease(
            self.version,
            ReleaseAsset(
                self.asset_name,
                f"https://github.com/example/{self.asset_name}",
                self.reported_size or self.archive.stat().st_size,
            ),
        )

    def download(self, _asset: ReleaseAsset, destination: Path, _cancel: Event) -> Path:
        self.downloads += 1
        shutil.copyfile(self.archive, destination)
        return destination


def manager_archive(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("release/cslol-manager.exe", b"new manager")
        archive.writestr("release/mod-tools.exe", b"new tools")


def trusted_archive(
    archive: Path,
    *,
    version: str = "2026.1",
) -> dict[tuple[str, str, int], str]:
    return {
        (version, "manager.zip", archive.stat().st_size): hashlib.sha256(
            archive.read_bytes()
        ).hexdigest()
    }


def make_updater(
    client: FakeReleaseClient,
    manager_dir: Path,
    *,
    trusted_assets: dict[tuple[str, str, int], str] | None = None,
    running: bool = False,
) -> ManagerUpdater:
    return ManagerUpdater(
        client,  # type: ignore[arg-type]
        manager_dir,
        manager_dir / "version.txt",
        lambda: running,
        logging.getLogger("test"),
        trusted_assets=trusted_assets or {},
    )


def test_updater_requires_version_marker_inside_manager_directory(tmp_path: Path) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"

    with pytest.raises(ValueError, match="direct child"):
        ManagerUpdater(
            FakeReleaseClient(archive),  # type: ignore[arg-type]
            manager_dir,
            tmp_path / "outside-version.txt",
            lambda: False,
            logging.getLogger("test"),
            trusted_assets=trusted_archive(archive),
        )


def test_manager_update_stages_and_preserves_user_content(tmp_path: Path) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"
    installed = manager_dir / "installed" / "user-mod"
    profiles = manager_dir / "profiles"
    installed.mkdir(parents=True)
    profiles.mkdir()
    (installed / "keep.txt").write_text("custom", encoding="utf-8")
    (profiles / "keep.profile").write_text("profile", encoding="utf-8")
    (manager_dir / "old.dll").write_bytes(b"old")
    updater = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )

    assert updater.update(Event()) is ManagerUpdateStatus.UPDATED
    assert (manager_dir / "cslol-manager.exe").read_bytes() == b"new manager"
    assert (manager_dir / "mod-tools.exe").read_bytes() == b"new tools"
    assert not (manager_dir / "old.dll").exists()
    assert (installed / "keep.txt").read_text(encoding="utf-8") == "custom"
    assert (profiles / "keep.profile").read_text(encoding="utf-8") == "profile"
    assert (manager_dir / "version.txt").read_text(encoding="utf-8") == "2026.1"


def test_manager_update_skips_current_running_and_cancelled(tmp_path: Path) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"
    manager_dir.mkdir()
    (manager_dir / "cslol-manager.exe").write_bytes(b"manager")
    version_file = manager_dir / "version.txt"
    version_file.write_text("2026.1", encoding="utf-8")
    client = FakeReleaseClient(archive)
    updater = make_updater(client, manager_dir)
    assert updater.update(Event()) is ManagerUpdateStatus.CURRENT
    assert client.downloads == 0

    version_file.write_text("old", encoding="utf-8")
    running = make_updater(client, manager_dir, running=True)
    assert running.update(Event()) is ManagerUpdateStatus.DEFERRED_RUNNING
    cancelled = Event()
    cancelled.set()
    assert updater.update(cancelled) is ManagerUpdateStatus.CANCELLED


def test_unreviewed_release_fails_before_download_and_preserves_live_manager(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "future.exe"
    archive.write_bytes(b"unreviewed executable")
    client = FakeReleaseClient(archive, version="future", asset_name="manager.exe")
    manager_dir = tmp_path / "live"
    manager_dir.mkdir()
    manager = manager_dir / "cslol-manager.exe"
    manager.write_bytes(b"old manager")
    updater = make_updater(client, manager_dir)

    with pytest.raises(UntrustedReleaseError, match="Install a reviewed release manually"):
        updater.update(Event())

    assert client.downloads == 0
    assert manager.read_bytes() == b"old manager"
    assert not updater.journal_path.exists()


def test_builtin_allowlist_is_exact_and_download_hash_is_rechecked(tmp_path: Path) -> None:
    version, name, size = next(iter(TRUSTED_RELEASE_ASSETS))
    fake_asset = tmp_path / name
    fake_asset.write_bytes(b"same metadata, wrong bytes")
    client = FakeReleaseClient(
        fake_asset,
        version=version,
        asset_name=name,
        reported_size=size,
    )
    manager_dir = tmp_path / "live"
    manager_dir.mkdir()
    old = manager_dir / "cslol-manager.exe"
    old.write_bytes(b"old")
    updater = make_updater(
        client,
        manager_dir,
        trusted_assets=dict(TRUSTED_RELEASE_ASSETS),
    )

    with pytest.raises(UntrustedReleaseError, match="SHA-256 mismatch"):
        updater.update(Event())

    assert client.downloads == 1
    assert old.read_bytes() == b"old"


def test_trusted_executable_is_verified_before_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = tmp_path / "trusted.exe"
    asset.write_bytes(b"reviewed sfx")
    client = FakeReleaseClient(asset, asset_name="trusted.exe")
    release = client.latest()
    trusted = {
        (release.version, release.asset.name, release.asset.size): hashlib.sha256(
            asset.read_bytes()
        ).hexdigest()
    }
    manager_dir = tmp_path / "live"
    updater = make_updater(client, manager_dir, trusted_assets=trusted)
    extracted: list[Path] = []

    def fake_extract(downloaded: Path, staged: Path, _cancel: Event) -> None:
        extracted.append(downloaded)
        (staged / "cslol-manager.exe").write_bytes(b"new manager")
        (staged / "mod-tools.exe").write_bytes(b"new tools")

    monkeypatch.setattr(updater, "_extract_trusted_sfx", fake_extract)

    assert updater.update(Event()) is ManagerUpdateStatus.UPDATED
    assert extracted
    assert (manager_dir / "cslol-manager.exe").read_bytes() == b"new manager"


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "C:/escape.exe",
        "release/file.exe:stream",
        "release/CON.txt",
        "release/trailing. ",
    ],
)
def test_safe_extract_rejects_windows_unsafe_names(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr(unsafe_name, b"bad")
    stream.seek(0)

    with zipfile.ZipFile(stream) as archive, pytest.raises(ValueError):
        _safe_extract(archive, tmp_path / "staged")


def test_safe_extract_rejects_case_collisions_symlinks_and_encryption(
    tmp_path: Path,
) -> None:
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as output:
        output.writestr("release/Tool.dll", b"one")
        output.writestr("release/tool.dll", b"two")
    duplicate.seek(0)
    with zipfile.ZipFile(duplicate) as archive, pytest.raises(ValueError, match="Duplicate"):
        _safe_extract(archive, tmp_path / "duplicate")

    symlink = io.BytesIO()
    with zipfile.ZipFile(symlink, "w") as output:
        link = zipfile.ZipInfo("release/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        output.writestr(link, "outside")
    symlink.seek(0)
    with zipfile.ZipFile(symlink) as archive, pytest.raises(ValueError, match="Symlink"):
        _safe_extract(archive, tmp_path / "symlink")

    encrypted = io.BytesIO()
    with zipfile.ZipFile(encrypted, "w") as output:
        output.writestr("release/encrypted.dll", b"secret")
    encrypted.seek(0)
    with zipfile.ZipFile(encrypted) as archive:
        archive.infolist()[0].flag_bits |= 0x1
        with pytest.raises(ValueError, match="Encrypted"):
            _safe_extract(archive, tmp_path / "encrypted")


def test_safe_extract_honours_limits_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as output:
        output.writestr("release/manager.exe", b"payload")
    stream.seek(0)
    monkeypatch.setattr(update_module, "MAX_ARCHIVE_MEMBER_BYTES", 1)
    with zipfile.ZipFile(stream) as archive, pytest.raises(ValueError, match="too large"):
        _safe_extract(archive, tmp_path / "large")

    monkeypatch.setattr(update_module, "MAX_ARCHIVE_MEMBER_BYTES", 1024)
    cancelled = Event()
    cancelled.set()
    stream.seek(0)
    with zipfile.ZipFile(stream) as archive, pytest.raises(InterruptedError, match="cancelled"):
        _safe_extract(archive, tmp_path / "cancelled", cancelled)


def test_state_write_failure_rolls_manager_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"
    manager_dir.mkdir()
    old_manager = manager_dir / "cslol-manager.exe"
    old_manager.write_bytes(b"old manager")
    version_file = manager_dir / "version.txt"
    version_file.write_text("old", encoding="utf-8")
    updater = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )
    real_atomic_write = update_module.atomic_write_text

    def fail_version(path: Path, value: str) -> None:
        if path == version_file:
            raise OSError("disk full")
        real_atomic_write(path, value)

    monkeypatch.setattr(update_module, "atomic_write_text", fail_version)

    with pytest.raises(ManagerTransactionError, match="restored"):
        updater.update(Event())

    assert old_manager.read_bytes() == b"old manager"
    assert version_file.read_text(encoding="utf-8") == "old"
    assert not updater.journal_path.exists()


def test_restart_recovers_interrupted_manager_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"
    manager_dir.mkdir()
    old_manager = manager_dir / "cslol-manager.exe"
    old_manager.write_bytes(b"old manager")
    version_file = manager_dir / "version.txt"
    version_file.write_text("old", encoding="utf-8")
    updater = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )

    def fail_version(_path: Path, _value: str) -> None:
        raise OSError("process stopped")

    def interrupted_rollback(_journal: object) -> None:
        raise RuntimeError("rollback interrupted")

    monkeypatch.setattr(update_module, "atomic_write_text", fail_version)
    monkeypatch.setattr(updater, "_rollback", interrupted_rollback)
    with pytest.raises(ManagerTransactionError, match="journal was preserved"):
        updater.update(Event())

    assert updater.journal_path.exists()
    assert old_manager.read_bytes() == b"new manager"
    monkeypatch.undo()

    restarted = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )
    assert restarted.recover()
    assert old_manager.read_bytes() == b"old manager"
    assert version_file.read_text(encoding="utf-8") == "old"
    assert not restarted.journal_path.exists()


def test_restart_keeps_committed_update_when_cleanup_was_interrupted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = tmp_path / "manager.zip"
    manager_archive(archive)
    manager_dir = tmp_path / "live"
    updater = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )
    real_rmtree = update_module.shutil.rmtree

    def fail_cleanup(path: Path, *args: object, **kwargs: object) -> None:
        candidate = Path(path)
        if candidate.name.startswith(update_module.UPDATE_TRANSACTION_PREFIX):
            raise OSError("busy")
        real_rmtree(candidate, *args, **kwargs)

    monkeypatch.setattr(update_module.shutil, "rmtree", fail_cleanup)
    assert updater.update(Event()) is ManagerUpdateStatus.UPDATED
    manager = manager_dir / "cslol-manager.exe"
    assert manager.read_bytes() == b"new manager"
    assert updater.journal_path.exists()
    monkeypatch.undo()

    restarted = make_updater(
        FakeReleaseClient(archive),
        manager_dir,
        trusted_assets=trusted_archive(archive),
    )
    assert restarted.recover()
    assert manager.read_bytes() == b"new manager"
    assert not restarted.journal_path.exists()
