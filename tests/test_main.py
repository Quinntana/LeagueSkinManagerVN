from __future__ import annotations

import logging
import sys
from pathlib import Path
from threading import Event
from typing import Any, cast

import pytest

import league_skin_manager.main as app_main
from league_skin_manager.config import AppPaths
from league_skin_manager.controller import AppController


class NoopActivationEvent:
    def create(self) -> bool:
        return True

    def signal(self) -> bool:
        return True

    def wait(self, _timeout_milliseconds: int) -> bool:
        return False

    def close(self) -> None:
        return None


def test_wait_for_workers_retries_without_releasing_ownership() -> None:
    class Controller:
        def __init__(self) -> None:
            self.results = iter((False, False, True))
            self.calls: list[float] = []

        def shutdown(self, timeout_seconds: float) -> bool:
            self.calls.append(timeout_seconds)
            return next(self.results)

    controller = Controller()

    app_main._wait_for_workers(
        cast(AppController, controller),
        0.25,
        logging.getLogger("test.main"),
    )

    assert controller.calls == [0.25, 0.25, 0.25]


def test_second_instance_signals_existing_desktop_without_launching_manager(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Paths:
        log_dir = tmp_path / "logs"
        manager_dir = tmp_path / "manager"

        @staticmethod
        def ensure() -> None:
            return None

    class Mutex:
        def acquire(self) -> bool:
            return False

    launches: list[Path] = []
    discoveries: list[bool] = []
    activations: list[str] = []

    class ActivationEvent(NoopActivationEvent):
        def create(self) -> bool:
            activations.append("create")
            return True

        def signal(self) -> bool:
            activations.append("signal")
            return True

        def close(self) -> None:
            activations.append("close")

    class Launcher:
        def __init__(self, _logger: logging.Logger) -> None:
            return None

        def launch(self, executable: Path) -> bool:
            launches.append(executable)
            return True

    def discover() -> Paths:
        discoveries.append(True)
        return Paths()

    monkeypatch.setattr(AppPaths, "discover", staticmethod(discover))
    monkeypatch.setattr(app_main, "configure_logging", lambda _path: logging.getLogger("test"))
    monkeypatch.setattr(app_main, "SingleInstanceMutex", Mutex)
    monkeypatch.setattr(app_main, "InstanceActivationEvent", ActivationEvent)
    monkeypatch.setattr(app_main, "ProcessLauncher", Launcher)
    monkeypatch.setattr(sys, "platform", "win32")

    assert app_main.run() == 2
    assert launches == []
    assert discoveries == []
    assert activations == ["create", "signal", "close"]


def test_activation_listener_forwards_signal_and_stops() -> None:
    stop = Event()
    waits: list[int] = []
    activations: list[bool] = []

    class ActivationEvent(NoopActivationEvent):
        def wait(self, timeout_milliseconds: int) -> bool:
            waits.append(timeout_milliseconds)
            return True

    def activate() -> None:
        activations.append(True)
        stop.set()

    app_main._listen_for_activation(
        cast(Any, ActivationEvent()),
        stop,
        activate,
        logging.getLogger("test.activation"),
    )

    assert waits == [250]
    assert activations == [True]


def test_activation_listener_logs_callback_and_wait_failures(caplog: Any) -> None:
    waits = 0

    class ActivationEvent(NoopActivationEvent):
        def wait(self, _timeout_milliseconds: int) -> bool:
            nonlocal waits
            waits += 1
            if waits == 1:
                return True
            raise OSError("event unavailable")

    def fail_activation() -> None:
        raise RuntimeError("window unavailable")

    with caplog.at_level(logging.ERROR):
        app_main._listen_for_activation(
            cast(Any, ActivationEvent()),
            Event(),
            fail_activation,
            logging.getLogger("test.activation.failure"),
        )

    assert waits == 2
    assert "Could not handle the application activation request" in caplog.text
    assert "activation listener failed" in caplog.text


def test_copy_cslol_manager_path_copies_root_not_installed_child(tmp_path: Path) -> None:
    manager_dir = tmp_path / "LeagueSkinManagerVN" / "cslol-manager"
    installed_dir = manager_dir / "installed"
    copied: list[str] = []
    notifications: list[tuple[str, str]] = []

    result = app_main._copy_cslol_manager_path(
        manager_dir,
        copied.append,
        lambda title, message: notifications.append((title, message)),
    )

    assert result == str(manager_dir)
    assert copied == [str(manager_dir)]
    assert copied[0] != str(installed_dir)
    assert notifications == [("CSLOL Manager folder copied", str(manager_dir))]


def test_refresh_ltk_port_status_publishes_exact_managed_counts(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Migration:
        def inspect_managed_port_status(self, selection: Path) -> object:
            assert selection == tmp_path / "installed"
            return type("Status", (), {"pending": 17, "total": 1907})()

    class Tray:
        def update_ltk_port_status(self, **values: object) -> None:
            calls.append(values)

    app_main._refresh_ltk_port_status(
        cast(Any, Migration()),
        tmp_path / "installed",
        cast(Any, Tray()),
        logging.getLogger("test.ltk_port"),
    )

    assert calls == [{"pending": 17, "total": 1907}]


@pytest.mark.parametrize(
    "error",
    [
        app_main.LtkMigrationError("migration ledger unavailable"),
        app_main.ManagedStateError("managed state unavailable"),
    ],
)
def test_refresh_ltk_port_status_fails_closed(
    tmp_path: Path,
    error: Exception,
    caplog: Any,
) -> None:
    calls: list[dict[str, object]] = []

    class Migration:
        def inspect_managed_port_status(self, _selection: Path) -> object:
            raise error

    class Tray:
        def update_ltk_port_status(self, **values: object) -> None:
            calls.append(values)

    with caplog.at_level(logging.WARNING):
        app_main._refresh_ltk_port_status(
            cast(Any, Migration()),
            tmp_path / "installed",
            cast(Any, Tray()),
            logging.getLogger("test.ltk_port.failure"),
        )

    assert calls == [{"pending": None, "total": None, "unavailable": True}]
    assert "Could not inspect the VN-to-LTK handoff state" in caplog.text


def test_wait_for_desktop_retries_until_optional_ui_stops() -> None:
    class Host:
        def __init__(self) -> None:
            self.results = iter((False, True))
            self.calls: list[float] = []

        def stop(self, timeout_seconds: float) -> bool:
            self.calls.append(timeout_seconds)
            return next(self.results)

    host = Host()

    app_main._wait_for_desktop(
        cast(Any, host),
        0.5,
        logging.getLogger("test.desktop_host"),
    )

    assert host.calls == [0.5, 0.5]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], (True, False)),
        (["--background"], (True, False)),
        (["--show-window"], (True, True)),
        (["--no-sync"], (False, False)),
    ],
)
def test_cli_is_tray_only_unless_window_is_explicitly_requested(
    monkeypatch: Any,
    arguments: list[str],
    expected: tuple[bool, bool],
) -> None:
    calls: list[tuple[bool, bool]] = []

    def fake_run(*, sync_on_start: bool, show_window: bool) -> int:
        calls.append((sync_on_start, show_window))
        return 7

    monkeypatch.setattr(app_main, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["LeagueSkinManagerVN", *arguments])

    with pytest.raises(SystemExit) as raised:
        app_main.main()

    assert raised.value.code == 7
    assert calls == [expected]


def test_composition_failure_closes_created_resources_and_releases_mutex(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    class Paths:
        log_dir = tmp_path / "logs"
        manager_dir = tmp_path / "manager"
        installed_dir = manager_dir / "installed"
        managed_manifest_file = tmp_path / "managed.json"
        package_cache_dir = tmp_path / "cache"
        manager_version_file = manager_dir / "version.txt"

        @staticmethod
        def ensure() -> None:
            return None

    released: list[bool] = []

    class Mutex:
        def acquire(self) -> bool:
            return True

        def release(self) -> None:
            released.append(True)

    closed: list[bool] = []

    class Source:
        def __init__(self, **_kwargs: object) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

    class Launcher:
        def __init__(self, _logger: logging.Logger) -> None:
            return None

    monkeypatch.setattr(AppPaths, "discover", staticmethod(lambda: Paths()))
    monkeypatch.setattr(app_main, "configure_logging", lambda _path: logging.getLogger("test"))
    monkeypatch.setattr(app_main, "SingleInstanceMutex", Mutex)
    monkeypatch.setattr(app_main, "InstanceActivationEvent", NoopActivationEvent)
    monkeypatch.setattr(app_main, "ProcessLauncher", Launcher)
    monkeypatch.setattr(app_main, "GitHubSkinSource", Source)
    monkeypatch.setattr(
        app_main,
        "SkinSyncService",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("composition failed")),
    )
    monkeypatch.setattr(sys, "platform", "win32")

    assert app_main.run() == 1
    assert closed == [True]
    assert released == [True]
