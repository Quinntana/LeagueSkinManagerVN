from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import psutil
import pytest

import league_skin_manager.uninstall as uninstall_module
from league_skin_manager.config import APP_NAME
from league_skin_manager.installation import InstallationError, InstallLayout
from league_skin_manager.uninstall import (
    BLOCKING_PROCESSES,
    RemovalState,
    Uninstaller,
    UninstallStatus,
    cleanup_relocated_copy,
    find_running_process,
    launch_installed_uninstaller_after_exit,
    launch_relocated_uninstaller,
    main,
    remove_install_tree,
    run_uninstall_entrypoint,
    wait_for_process_exit,
)


def app_paths(tmp_path: Path) -> tuple[Path, Path]:
    appdata = tmp_path / "AppData" / "Roaming"
    data_dir = appdata / APP_NAME
    data_dir.mkdir(parents=True)
    return appdata, data_dir


class FakeMutex:
    def __init__(
        self,
        name: str = "mutex",
        events: list[str] | None = None,
        *,
        acquired: bool = True,
    ) -> None:
        self.name = name
        self.events = events if events is not None else []
        self.acquired = acquired
        self.releases = 0

    def acquire(self) -> bool:
        self.events.append(f"{self.name}:acquire")
        return self.acquired

    def release(self) -> None:
        self.releases += 1
        self.events.append(f"{self.name}:release")


def test_running_manager_aborts_before_any_cleanup_and_releases_both_gates(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    cleanup_calls: list[str] = []
    events: list[str] = []
    operation = FakeMutex("operation", events)
    app = FakeMutex("app", events)

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: "cslol-manager.exe",
        startup_remover=lambda: cleanup_calls.append("startup") or RemovalState.REMOVED,
        tree_remover=lambda _path: cleanup_calls.append("data") or RemovalState.REMOVED,
        operation_mutex=operation,
        mutex=app,
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert result.blocking_process == "cslol-manager.exe"
    assert cleanup_calls == []
    assert events == [
        "operation:acquire",
        "app:acquire",
        "app:release",
        "operation:release",
    ]


def test_success_holds_both_gates_and_removes_registration_after_install_files(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: events.append("scan") or None,
        startup_remover=lambda: events.append("startup") or RemovalState.REMOVED,
        tree_remover=lambda _path: events.append("data") or RemovalState.REMOVED,
        install_cleanup=lambda: events.append("install") or RemovalState.REMOVED,
        registration_remover=lambda: events.append("registration") or RemovalState.REMOVED,
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events),
    ).run()

    assert result.ok
    assert result.install_files is RemovalState.REMOVED
    assert result.registration is RemovalState.REMOVED
    assert events == [
        "operation:acquire",
        "app:acquire",
        "scan",
        "startup",
        "data",
        "install",
        "registration",
        "app:release",
        "operation:release",
    ]


def test_uninstall_removes_ltk_cache_and_history_but_preserves_external_ltk_data(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    (data_dir / "cache" / "ltk").mkdir(parents=True)
    (data_dir / "cache" / "ltk" / "installer.exe").write_bytes(b"cache")
    (data_dir / "ltk_migration_state.json").write_text("{}", encoding="utf-8")
    external_ltk = appdata / "dev.leaguetoolkit.manager"
    external_ltk.mkdir()
    sentinel = external_ltk / "library.json"
    sentinel.write_text("external", encoding="utf-8")

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: None,
        startup_remover=lambda: RemovalState.NOT_FOUND,
    ).run()

    assert result.ok
    assert not data_dir.exists()
    assert sentinel.read_text(encoding="utf-8") == "external"


def test_interactive_uninstall_removes_complete_owned_layout_and_keeps_neighbors(
    tmp_path: Path,
) -> None:
    appdata, data_dir = app_paths(tmp_path)
    local_appdata = tmp_path / "AppData" / "Local"
    layout = InstallLayout.discover(local_appdata)
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstaller")
    (layout.install_dir / "obsolete.bin").write_bytes(b"old")
    for relative in (
        "cache/packages/skin.fantome",
        "cache/ltk/LTK-setup.exe",
        "cslol-manager/installed/skin/WAD/skin.wad.client",
        "cslol-manager/profiles/Default Profile.config",
        "logs/LeagueSkinManagerVN.log",
        "migration-reports/report.json",
        "managed_skins.json",
        "ltk_migration_state.json",
        "ltk_archive_index.json",
    ):
        target = data_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"owned")
    external_ltk = appdata / "dev.leaguetoolkit.manager"
    external_ltk.mkdir()
    external_skin = external_ltk / "archives" / "keep.fantome"
    external_skin.parent.mkdir()
    external_skin.write_bytes(b"external")
    local_neighbor = local_appdata / "Programs" / "OtherApp" / "keep.exe"
    local_neighbor.parent.mkdir(parents=True)
    local_neighbor.write_bytes(b"neighbor")
    notifications: list[tuple[str, str, bool]] = []

    exit_code = main(
        appdata=appdata,
        local_appdata=local_appdata,
        confirmer=lambda _title, _message: True,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        process_finder=lambda _names: None,
        startup_remover=lambda: RemovalState.REMOVED,
        registration_remover=lambda: RemovalState.REMOVED,
        operation_mutex=FakeMutex(),
        mutex=FakeMutex(),
    )

    assert exit_code == 0
    assert not data_dir.exists()
    assert not layout.install_dir.exists()
    assert external_skin.read_bytes() == b"external"
    assert local_neighbor.read_bytes() == b"neighbor"
    assert notifications[-1][0] == "Uninstall complete"
    assert notifications[-1][2] is False


def test_install_file_failure_preserves_apps_registration_for_retry(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    registration_calls: list[str] = []

    def fail_install() -> RemovalState:
        raise PermissionError("file locked")

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: None,
        startup_remover=lambda: RemovalState.REMOVED,
        tree_remover=lambda _path: RemovalState.REMOVED,
        install_cleanup=fail_install,
        registration_remover=lambda: (
            registration_calls.append("registration") or RemovalState.REMOVED
        ),
    ).run()

    assert result.status is UninstallStatus.PARTIAL
    assert result.install_files is RemovalState.FAILED
    assert result.registration is RemovalState.SKIPPED
    assert registration_calls == []


def test_operation_gate_blocks_before_app_gate_or_cleanup(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: events.append("scan") or None,
        operation_mutex=FakeMutex("operation", events, acquired=False),
        mutex=FakeMutex("app", events),
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert events == ["operation:acquire"]


def test_app_gate_failure_releases_operation_gate(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []

    result = Uninstaller(
        appdata_root=appdata,
        data_dir=data_dir,
        process_finder=lambda _names: None,
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events, acquired=False),
    ).run()

    assert result.status is UninstallStatus.ABORTED
    assert events == ["operation:acquire", "app:acquire", "operation:release"]


def test_uninstaller_rejects_every_target_except_appdata_app_folder(tmp_path: Path) -> None:
    appdata = tmp_path / "AppData" / "Roaming"
    appdata.mkdir(parents=True)

    with pytest.raises(ValueError, match="outside APPDATA"):
        Uninstaller(appdata_root=appdata, data_dir=tmp_path / APP_NAME)

    with pytest.raises(ValueError, match="must be named"):
        Uninstaller(appdata_root=appdata, data_dir=appdata / "NotTheApplication")


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


def test_process_detection_blocks_helpers_inside_owned_data_root(tmp_path: Path) -> None:
    data_dir = tmp_path / APP_NAME
    helper = data_dir / "cslol-manager" / "cslol-tools" / "mod-tools.exe"
    processes = [FakeProcess({"pid": 50, "name": "mod-tools.exe", "exe": str(helper)})]

    found = find_running_process(
        (),
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
        blocked_roots=(data_dir,),
    )

    assert found == "mod-tools.exe"


def test_process_detection_blocks_known_helper_when_path_is_inaccessible() -> None:
    processes = [FakeProcess({"pid": 50, "name": "MOD-TOOLS.EXE", "exe": None})]

    found = find_running_process(
        BLOCKING_PROCESSES,
        process_iter=lambda _fields: iter(processes),
        current_pid=99,
    )

    assert found == "MOD-TOOLS.EXE"


def test_remove_install_tree_deletes_only_validated_program_directory(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")
    outside = tmp_path / "keep.txt"
    outside.write_text("keep", encoding="utf-8")

    state = remove_install_tree(layout)

    assert state is RemovalState.REMOVED
    assert not layout.install_dir.exists()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_tray_launches_exact_installed_uninstaller_after_application_exit(
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstaller")
    calls: list[tuple[list[str], dict[str, object]]] = []

    class Process:
        pid = 987

    def launch(args: list[str], **kwargs: object) -> Process:
        calls.append((args, kwargs))
        return Process()

    pid = launch_installed_uninstaller_after_exit(
        layout,
        wait_pid=321,
        popen=launch,
    )

    assert pid == 987
    assert calls[0][0] == [str(layout.uninstaller.resolve())]
    assert calls[0][1]["cwd"] == str(layout.install_dir)
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert environment["LSMVN_UNINSTALL_WAIT_PID"] == "321"
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_tray_uninstall_launch_rejects_missing_payload_and_bad_pid(tmp_path: Path) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)

    with pytest.raises(InstallationError, match="missing or unsafe"):
        launch_installed_uninstaller_after_exit(layout, wait_pid=123)

    layout.uninstaller.write_bytes(b"uninstaller")
    with pytest.raises(ValueError, match="process identifier"):
        launch_installed_uninstaller_after_exit(layout, wait_pid=0)


def test_tray_uninstall_launch_rejects_reparse_payload(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.uninstaller.write_bytes(b"uninstaller")
    original = uninstall_module._is_reparse_point
    monkeypatch.setattr(
        uninstall_module,
        "_is_reparse_point",
        lambda path: path == layout.uninstaller or original(path),
    )

    with pytest.raises(InstallationError, match="missing or unsafe"):
        launch_installed_uninstaller_after_exit(layout, wait_pid=123)


def test_relocation_copies_installed_uninstaller_and_passes_bootloader_pid(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    local = tmp_path / "LocalAppData"
    layout = InstallLayout.discover(local)
    layout.install_dir.mkdir(parents=True)
    layout.uninstaller.write_bytes(b"uninstaller")
    layout.executable.write_bytes(b"main")
    temp_root = tmp_path / "Temp"
    calls: list[tuple[list[str], dict[str, object]]] = []
    monkeypatch.setattr(uninstall_module.os, "name", "nt")

    temp_dir = launch_relocated_uninstaller(
        layout,
        executable=layout.uninstaller,
        parent_pid=321,
        temp_root=temp_root,
        popen=lambda args, **kwargs: calls.append((args, kwargs)),
    )

    relocated = temp_dir / layout.uninstaller.name
    assert relocated.read_bytes() == b"uninstaller"
    assert calls[0][0] == [str(relocated)]
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert environment["LSMVN_UNINSTALL_RELOCATED"] == "1"
    assert environment["LSMVN_UNINSTALL_WAIT_PID"] == "321"
    assert environment["LSMVN_UNINSTALL_TEMP_DIR"] == str(temp_dir)
    assert environment["PYINSTALLER_RESET_ENVIRONMENT"] == "1"


def test_wait_for_original_bootloader_parent() -> None:
    calls: list[float] = []

    class Process:
        def wait(self, timeout: float) -> None:
            calls.append(timeout)

    wait_for_process_exit(321, timeout_seconds=4.5, process_factory=lambda _pid: Process())

    assert calls == [4.5]


def test_frozen_installed_entrypoint_relocates_before_interactive_main(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    layout.install_dir.mkdir(parents=True)
    layout.executable.write_bytes(b"main")
    layout.uninstaller.write_bytes(b"uninstall")
    events: list[str] = []
    monkeypatch.delenv("LSMVN_UNINSTALL_RELOCATED", raising=False)
    monkeypatch.delenv("LSMVN_UNINSTALL_TEMP_DIR", raising=False)
    monkeypatch.delenv("LSMVN_UNINSTALL_WAIT_PID", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(layout.uninstaller))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        uninstall_module,
        "launch_relocated_uninstaller",
        lambda *_args, **_kwargs: events.append("relocate") or tmp_path,
    )
    monkeypatch.setattr(uninstall_module, "main", lambda: events.append("main") or 0)

    result = run_uninstall_entrypoint()

    assert result == 0
    assert events == ["relocate"]


def test_relocated_entrypoint_waits_for_original_parent_then_runs_main(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    layout = InstallLayout.discover(tmp_path / "LocalAppData")
    temp_dir = Path(os.environ.get("TEMP", str(tmp_path))) / f"{APP_NAME}-uninstall-test"
    events: list[str] = []
    monkeypatch.setenv("LSMVN_UNINSTALL_RELOCATED", "1")
    monkeypatch.setenv("LSMVN_UNINSTALL_WAIT_PID", "654")
    monkeypatch.delenv("LSMVN_UNINSTALL_TEMP_DIR", raising=False)
    monkeypatch.setattr(uninstall_module.InstallLayout, "discover", lambda: layout)
    monkeypatch.setattr(uninstall_module.sys, "executable", str(temp_dir / "uninstall.exe"))
    monkeypatch.setattr(uninstall_module.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        uninstall_module,
        "wait_for_process_exit",
        lambda pid: events.append(f"wait:{pid}"),
    )
    monkeypatch.setattr(uninstall_module, "main", lambda: events.append("main") or 0)

    result = run_uninstall_entrypoint()

    assert result == 0
    assert events == ["wait:654", "main"]


def test_cleanup_relocated_copy_removes_unlocked_temp_directory(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(uninstall_module.tempfile, "gettempdir", lambda: str(tmp_path))
    temp_dir = tmp_path / f"{APP_NAME}-uninstall-test"
    temp_dir.mkdir()
    (temp_dir / "copy.exe").write_bytes(b"copy")

    cleanup_relocated_copy(temp_dir)

    assert not temp_dir.exists()


def test_main_cancel_returns_normally_without_acquiring_gates(tmp_path: Path) -> None:
    appdata, data_dir = app_paths(tmp_path)
    events: list[str] = []
    notifications: list[tuple[str, str, bool]] = []

    result = main(
        appdata=appdata,
        local_appdata=tmp_path / "LocalAppData",
        confirmer=lambda _title, _message: False,
        notifier=lambda title, message, error: notifications.append((title, message, error)),
        operation_mutex=FakeMutex("operation", events),
        mutex=FakeMutex("app", events),
    )

    assert result == 0
    assert events == []
    assert data_dir.exists()
    assert notifications == [("Uninstall cancelled", "Nothing was removed.", False)]
