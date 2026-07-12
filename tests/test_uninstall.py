from __future__ import annotations

from pathlib import Path
from typing import Any

import psutil
import pytest

from league_skin_manager.config import APP_NAME
from league_skin_manager.uninstall import (
    BLOCKING_PROCESSES,
    RemovalState,
    Uninstaller,
    UninstallStatus,
    find_running_process,
    main,
)


def app_paths(tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "AppData" / "Roaming"
    data_dir = appdata / APP_NAME
    data_dir.mkdir(parents=True)
    return appdata, data_dir


def test_running_manager_aborts_before_any_cleanup(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    marker = data_dir / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    cleanup_calls: list[str] = []

    uninstaller = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda names: "cslol-manager.exe" if "cslol-manager.exe" in names else None,
        startup_remover=lambda: cleanup_calls.append("startup") or RemovalState.REMOVED,
        tree_remover=lambda _path: cleanup_calls.append("data") or RemovalState.REMOVED,
    )

    result = uninstaller.run()

    assert result.status is UninstallStatus.ABORTED
    assert result.blocking_process == "cslol-manager.exe"
    assert result.startup is RemovalState.SKIPPED
    assert result.app_data is RemovalState.SKIPPED
    assert cleanup_calls == []
    assert marker.read_text(encoding="utf-8") == "keep"


def test_success_removes_startup_and_app_data(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    (data_dir / "cache").mkdir()
    (data_dir / "cache" / "skin.fantome").write_bytes(b"skin")

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda names: None,
        startup_remover=lambda: RemovalState.REMOVED,
    ).run()

    assert result.ok
    assert result.status is UninstallStatus.SUCCESS
    assert result.startup is RemovalState.REMOVED
    assert result.app_data is RemovalState.REMOVED
    assert not data_dir.exists()
    assert "Uninstall complete" in result.message


def test_cleanup_failure_is_reported_but_other_cleanup_continues(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)

    def fail_startup() -> RemovalState:
        raise PermissionError("registry denied")

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda names: None,
        startup_remover=fail_startup,
    ).run()

    assert result.status is UninstallStatus.PARTIAL
    assert result.startup is RemovalState.FAILED
    assert result.app_data is RemovalState.REMOVED
    assert result.errors == ("startup registration: registry denied",)
    assert not data_dir.exists()


def test_uninstaller_rejects_every_target_except_appdata_app_folder(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside APPDATA"):
        Uninstaller(
            appdata_root=appdata,
            data_dir=tmp_path / APP_NAME,
            process_finder=lambda names: None,
        )

    with pytest.raises(ValueError, match="must be named"):
        Uninstaller(
            appdata_root=appdata,
            data_dir=appdata / "NotTheApplication",
            process_finder=lambda names: None,
        )


class FakeProcess:
    def __init__(self, info: dict[str, Any] | BaseException) -> None:
        self._info = info

    @property
    def info(self) -> dict[str, Any]:
        if isinstance(self._info, BaseException):
            raise self._info
        return self._info


def test_process_detection_is_case_insensitive_and_tolerates_access_denied() -> None:
    processes = [
        FakeProcess(psutil.AccessDenied(pid=1)),
        FakeProcess({"pid": 50, "name": "CSLOL-MANAGER.EXE"}),
    ]

    found = find_running_process(
        BLOCKING_PROCESSES,
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
    )

    assert found == "CSLOL-MANAGER.EXE"


def test_main_cancel_returns_normally_without_cleanup(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    notifications: list[tuple[str, str, bool]] = []
    cleanup_calls: list[str] = []

    result = main(
        appdata=appdata,
        confirmer=lambda _title, _message: False,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        startup_remover=lambda: cleanup_calls.append("startup") or RemovalState.REMOVED,
        tree_remover=lambda _path: cleanup_calls.append("data") or RemovalState.REMOVED,
    )

    assert result == 0
    assert cleanup_calls == []
    assert data_dir.exists()
    assert notifications == [("Uninstall cancelled", "Nothing was removed.", False)]
