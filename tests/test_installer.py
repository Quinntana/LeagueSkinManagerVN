from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import league_skin_manager.installer as installer_module
from league_skin_manager.config import APP_NAME, UNINSTALL_APP_NAME
from league_skin_manager.installation import InstallationError, InstallLayout
from league_skin_manager.installer import (
    InstallResult,
    confirm_install,
    install_payload,
    main,
    payload_paths,
)


class Registration:
    def __init__(self) -> None:
        self.calls: list[tuple[InstallLayout, int]] = []

    def register(self, layout: InstallLayout, *, estimated_size_kib: int) -> None:
        self.calls.append((layout, estimated_size_kib))


def test_setup_confirmation_defaults_to_no(monkeypatch: Any) -> None:
    calls: list[tuple[object, str, str, int]] = []

    class User32:
        @staticmethod
        def MessageBoxW(owner: object, message: str, title: str, flags: int) -> int:
            calls.append((owner, message, title, flags))
            return 6

    monkeypatch.setattr(installer_module.os, "name", "nt")
    monkeypatch.setattr(
        installer_module.ctypes,
        "windll",
        SimpleNamespace(user32=User32()),
        raising=False,
    )

    assert confirm_install("Setup", "Install?") is True
    assert calls[0][3] & 0x00000100


def payload(
    tmp_path: Path,
    main: bytes = b"main",
    uninstall: bytes = b"uninstall",
) -> tuple[Path, Path]:
    main_path = tmp_path / f"{APP_NAME}.exe"
    uninstall_path = tmp_path / f"{UNINSTALL_APP_NAME}.exe"
    main_path.write_bytes(main)
    uninstall_path.write_bytes(uninstall)
    return main_path, uninstall_path


def test_installer_atomically_replaces_existing_program_and_registers(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main_source, uninstall_source = payload(source)
    layout = InstallLayout.discover(tmp_path / "local")
    layout.install_dir.mkdir(parents=True)
    (layout.install_dir / "obsolete.txt").write_text("old", encoding="utf-8")
    registration = Registration()

    result = install_payload(
        main_source,
        uninstall_source,
        layout,
        registration=registration,
    )

    assert result.executable.read_bytes() == b"main"
    assert result.uninstaller.read_bytes() == b"uninstall"
    assert not (layout.install_dir / "obsolete.txt").exists()
    assert registration.calls[0][0] == layout
    assert registration.calls[0][1] == 1
    assert not list(layout.install_dir.parent.glob(f".{APP_NAME}-*"))


def test_incomplete_payload_is_rejected_without_touching_existing_install(
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "local")
    layout.install_dir.mkdir(parents=True)
    marker = layout.install_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    missing = tmp_path / "missing.exe"

    with pytest.raises(InstallationError, match="missing"):
        install_payload(missing, missing, layout)

    assert marker.read_text(encoding="utf-8") == "keep"


def test_payload_lookup_prefers_bundled_directory_and_rejects_missing(tmp_path: Path) -> None:
    bundled = tmp_path / "payload"
    bundled.mkdir()
    expected = payload(bundled)
    assert payload_paths(tmp_path) == expected

    expected[0].unlink()
    with pytest.raises(InstallationError, match="incomplete"):
        payload_paths(tmp_path)


def test_registration_failure_restores_previous_program_files(tmp_path: Path) -> None:
    class FailingRegistration:
        def register(self, _layout: InstallLayout, *, estimated_size_kib: int) -> None:
            assert estimated_size_kib > 0
            raise PermissionError("registry denied")

    source = tmp_path / "source"
    source.mkdir()
    main_source, uninstall_source = payload(source)
    layout = InstallLayout.discover(tmp_path / "local")
    layout.install_dir.mkdir(parents=True)
    marker = layout.install_dir / "previous.txt"
    marker.write_text("previous", encoding="utf-8")

    with pytest.raises(PermissionError, match="registry denied"):
        install_payload(
            main_source,
            uninstall_source,
            layout,
            registration=FailingRegistration(),
        )

    assert marker.read_text(encoding="utf-8") == "previous"
    assert not layout.executable.exists()


def test_old_backup_cleanup_failure_does_not_turn_install_into_failure(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    main_source, uninstall_source = payload(source)
    layout = InstallLayout.discover(tmp_path / "local")
    layout.install_dir.mkdir(parents=True)
    (layout.install_dir / "old.txt").write_text("old", encoding="utf-8")
    original_rmtree = installer_module.shutil.rmtree

    def fail_only_for_backup(path: Path, *args: Any, **kwargs: Any) -> None:
        if "-backup-" in Path(path).name:
            raise PermissionError("backup locked")
        original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(installer_module.shutil, "rmtree", fail_only_for_backup)

    result = install_payload(
        main_source,
        uninstall_source,
        layout,
        registration=Registration(),
    )

    assert result.executable.read_bytes() == b"main"
    assert list(layout.install_dir.parent.glob(f".{APP_NAME}-backup-*"))


class RecordingMutex:
    def __init__(self, name: str, events: list[str], *, acquired: bool = True) -> None:
        self.name = name
        self.events = events
        self.acquired = acquired

    def acquire(self) -> bool:
        self.events.append(f"{self.name}:acquire")
        return self.acquired

    def release(self) -> None:
        self.events.append(f"{self.name}:release")


def test_setup_serializes_install_and_releases_app_gate_only_for_launch(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    source = tmp_path / "payload-root"
    source.mkdir()
    payload(source)
    layout = InstallLayout.discover(tmp_path / "local")
    monkeypatch.setattr(installer_module.sys, "platform", "win32")

    def fake_install(*_args: Any, **_kwargs: Any) -> InstallResult:
        events.append("install")
        return InstallResult(layout.install_dir, layout.executable, layout.uninstaller)

    monkeypatch.setattr(installer_module, "install_payload", fake_install)
    notifications: list[tuple[str, str, bool]] = []

    result = main(
        local_appdata=layout.local_appdata,
        payload_root=source,
        confirmer=lambda _title, _message: events.append("confirm") or True,
        notifier=lambda title, message, error: (
            events.append("notify"),
            notifications.append((title, message, error)),
        ),
        launcher=lambda _path: events.append("launch"),
        process_finder=lambda _names: events.append("scan") or None,
        operation_mutex=RecordingMutex("operation", events),
        app_mutex=RecordingMutex("app", events),
    )

    assert result == 0
    assert events == [
        "confirm",
        "operation:acquire",
        "app:acquire",
        "scan",
        "install",
        "app:release",
        "launch",
        "notify",
        "operation:release",
    ]
    assert notifications[0][0] == "Setup complete"
    assert not notifications[0][2]


def test_launch_failure_is_post_install_warning_and_success(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    source = tmp_path / "payload-root"
    source.mkdir()
    payload(source)
    layout = InstallLayout.discover(tmp_path / "local")
    monkeypatch.setattr(installer_module.sys, "platform", "win32")
    monkeypatch.setattr(
        installer_module,
        "install_payload",
        lambda *_args, **_kwargs: InstallResult(
            layout.install_dir,
            layout.executable,
            layout.uninstaller,
        ),
    )
    notifications: list[tuple[str, str, bool]] = []

    def fail_launch(_path: Path) -> None:
        raise OSError("launch blocked")

    result = main(
        local_appdata=layout.local_appdata,
        payload_root=source,
        confirmer=lambda _title, _message: True,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        launcher=fail_launch,
        process_finder=lambda _names: None,
        operation_mutex=RecordingMutex("operation", events),
        app_mutex=RecordingMutex("app", events),
    )

    assert result == 0
    assert notifications[0][0] == "Setup complete"
    assert "launch blocked" in notifications[0][1]
    assert notifications[0][2]
    assert events == [
        "operation:acquire",
        "app:acquire",
        "app:release",
        "operation:release",
    ]
