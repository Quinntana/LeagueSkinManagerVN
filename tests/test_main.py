from __future__ import annotations

import json
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


def test_second_instance_signals_the_running_tray_without_launching_manager(
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


def test_refresh_ltk_library_summary_publishes_counts_and_drift(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Reconciler:
        def resolve_storage_dir(self) -> Path:
            return tmp_path / "ltk"

        def inspect_baseline(self) -> object:
            return type("Baseline", (), {"expected": 1907, "missing": 5, "extra": 2})()

    class Tray:
        def update_ltk_library(self, **values: object) -> None:
            calls.append(values)

    library = tmp_path / "ltk"
    library.mkdir()
    (library / "library.json").write_text(
        json.dumps(
            {
                "mods": [{"id": "one"}, {"id": "two"}],
                "profiles": [{"id": "p", "enabledMods": ["one"]}],
                "activeProfileId": "p",
            }
        ),
        encoding="utf-8",
    )

    app_main._refresh_ltk_library_summary(
        cast(Any, Reconciler()),
        cast(Any, Tray()),
        lambda: True,
        logging.getLogger("test.ltk_library"),
    )

    assert calls == [
        {
            "installed": True,
            "in_library": 2,
            "enabled": 1,
            "expected": 1907,
            "pending": 7,
        }
    ]


def test_refresh_ltk_library_summary_reports_a_missing_install(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Tray:
        def update_ltk_library(self, **values: object) -> None:
            calls.append(values)

    class Reconciler:
        def resolve_storage_dir(self) -> Path:
            return tmp_path / "ltk"

    app_main._refresh_ltk_library_summary(
        cast(Any, Reconciler()),
        cast(Any, Tray()),
        lambda: False,
        logging.getLogger("test.ltk_library.absent"),
    )

    assert calls == [{"installed": False}]


def test_refresh_ltk_library_summary_reports_an_unreadable_library(tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    class Tray:
        def update_ltk_library(self, **values: object) -> None:
            calls.append(values)

    class Reconciler:
        def resolve_storage_dir(self) -> Path:
            return tmp_path / "missing-storage"

    app_main._refresh_ltk_library_summary(
        cast(Any, Reconciler()),
        cast(Any, Tray()),
        lambda: True,
        logging.getLogger("test.ltk_library.unreadable"),
    )

    assert calls == [{"installed": True}]


def test_refresh_ltk_library_summary_falls_back_when_drift_is_unavailable(
    tmp_path: Path,
    caplog: Any,
) -> None:
    calls: list[dict[str, object]] = []

    class Reconciler:
        def resolve_storage_dir(self) -> Path:
            return tmp_path / "ltk"

        def inspect_baseline(self) -> object:
            raise app_main.LtkReconcileError("package cache unavailable")

    class Tray:
        def update_ltk_library(self, **values: object) -> None:
            calls.append(values)

    library = tmp_path / "ltk"
    library.mkdir()
    (library / "library.json").write_text(json.dumps({"mods": []}), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        app_main._refresh_ltk_library_summary(
            cast(Any, Reconciler()),
            cast(Any, Tray()),
            lambda: True,
            logging.getLogger("test.ltk_library.drift"),
        )

    assert calls == [{"installed": True, "in_library": 0, "enabled": 0}]
    assert "Could not compare LTK with the current skin set" in caplog.text


def test_wait_for_window_retries_until_optional_ui_stops() -> None:
    class Host:
        def __init__(self) -> None:
            self.results = iter((False, True))
            self.calls: list[float] = []

        def stop(self, timeout_seconds: float) -> bool:
            self.calls.append(timeout_seconds)
            return next(self.results)

    host = Host()

    app_main._wait_for_window(
        cast(Any, host),
        0.5,
        logging.getLogger("test.window_host"),
    )

    assert host.calls == [0.5, 0.5]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], True),
        (["--background"], True),
        (["--no-sync"], False),
    ],
)
def test_cli_is_tray_only(
    monkeypatch: Any,
    arguments: list[str],
    expected: bool,
) -> None:
    calls: list[bool] = []

    def fake_run(*, sync_on_start: bool) -> int:
        calls.append(sync_on_start)
        return 7

    monkeypatch.setattr(app_main, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["LeagueSkinManagerVN", *arguments])

    with pytest.raises(SystemExit) as raised:
        app_main.main()

    assert raised.value.code == 7
    assert calls == [expected]


@pytest.mark.parametrize("flag", ["--show-window", "--browse"])
def test_removed_presentation_flags_are_rejected(monkeypatch: Any, flag: str) -> None:
    monkeypatch.setattr(sys, "argv", ["LeagueSkinManagerVN", flag])

    with pytest.raises(SystemExit) as raised:
        app_main.main()

    assert raised.value.code == 2


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


class RecordingLauncher:
    def __init__(self, *, launch_results: dict[str, bool], running_under: bool = False) -> None:
        self.launch_results = launch_results
        self.running_under = running_under
        self.launched: list[Path] = []

    def launch(self, executable: Path) -> bool:
        self.launched.append(executable)
        return self.launch_results.get(executable.name, False)

    def is_running_under(self, _directory: Path) -> bool:
        return self.running_under


class LocatedLtk:
    def __init__(self, executable: Path) -> None:
        self.executable = executable


def test_preferred_manager_launch_uses_installed_ltk_first(tmp_path: Path) -> None:
    ltk_exe = tmp_path / "LTK Manager" / "ltk-manager.exe"
    launcher = RecordingLauncher(launch_results={"ltk-manager.exe": True})

    assert app_main._launch_preferred_manager(
        launcher=cast(Any, launcher),
        manager_dir=tmp_path / "cslol-manager",
        manager_executable=tmp_path / "cslol-manager" / "cslol-manager.exe",
        locate_ltk=lambda: LocatedLtk(ltk_exe),
        ltk_is_running=lambda: False,
        logger=logging.getLogger("test.main"),
    )

    assert launcher.launched == [ltk_exe]


def test_preferred_manager_launch_falls_back_to_cslol_without_ltk(tmp_path: Path) -> None:
    cslol_exe = tmp_path / "cslol-manager" / "cslol-manager.exe"
    launcher = RecordingLauncher(launch_results={"cslol-manager.exe": True})

    assert app_main._launch_preferred_manager(
        launcher=cast(Any, launcher),
        manager_dir=tmp_path / "cslol-manager",
        manager_executable=cslol_exe,
        locate_ltk=lambda: None,
        ltk_is_running=lambda: False,
        logger=logging.getLogger("test.main"),
    )

    assert launcher.launched == [cslol_exe]


def test_preferred_manager_launch_falls_back_when_ltk_fails_to_start(tmp_path: Path) -> None:
    ltk_exe = tmp_path / "LTK Manager" / "ltk-manager.exe"
    cslol_exe = tmp_path / "cslol-manager" / "cslol-manager.exe"
    launcher = RecordingLauncher(launch_results={"cslol-manager.exe": True})

    assert app_main._launch_preferred_manager(
        launcher=cast(Any, launcher),
        manager_dir=tmp_path / "cslol-manager",
        manager_executable=cslol_exe,
        locate_ltk=lambda: LocatedLtk(ltk_exe),
        ltk_is_running=lambda: False,
        logger=logging.getLogger("test.main"),
    )

    assert launcher.launched == [ltk_exe, cslol_exe]


def test_preferred_manager_launch_short_circuits_when_ltk_is_running(tmp_path: Path) -> None:
    launcher = RecordingLauncher(launch_results={})

    assert app_main._launch_preferred_manager(
        launcher=cast(Any, launcher),
        manager_dir=tmp_path / "cslol-manager",
        manager_executable=tmp_path / "cslol-manager" / "cslol-manager.exe",
        locate_ltk=lambda: LocatedLtk(tmp_path / "ltk-manager.exe"),
        ltk_is_running=lambda: True,
        logger=logging.getLogger("test.main"),
    )

    assert launcher.launched == []


def test_league_exit_observer_signals_only_on_exit_transitions() -> None:
    class InnerMonitor:
        def run(self, _stop_event: Event, changed: Any) -> None:
            changed(101)
            changed(None)
            changed(202)
            changed(None)

    seen: list[int | None] = []
    exits: list[str] = []
    observer = app_main._LeagueExitObserver(
        cast(Any, InnerMonitor()),
        lambda: exits.append("exit"),
        logging.getLogger("test.main"),
    )

    observer.run(Event(), seen.append)

    assert seen == [101, None, 202, None]
    assert exits == ["exit", "exit"]


def test_league_exit_observer_swallows_follow_up_failures(caplog: Any) -> None:
    class InnerMonitor:
        def run(self, _stop_event: Event, changed: Any) -> None:
            changed(None)

    def failing_follow_up() -> None:
        raise RuntimeError("retry queue unavailable")

    caplog.set_level(logging.ERROR, logger="test.main")
    observer = app_main._LeagueExitObserver(
        cast(Any, InnerMonitor()),
        failing_follow_up,
        logging.getLogger("test.main"),
    )

    observer.run(Event(), lambda _pid: None)

    assert "League-exit follow-up work failed" in caplog.text


def test_startup_library_summary_skips_the_drift_comparison(tmp_path: Path) -> None:
    """The tray must appear before anything inspects LTK's storage."""

    calls: list[dict[str, object]] = []
    inspections: list[str] = []

    class Reconciler:
        def resolve_storage_dir(self) -> Path:
            return tmp_path / "ltk"

        def inspect_baseline(self) -> object:
            inspections.append("inspected")
            raise AssertionError("drift must not be computed on the startup path")

    class Tray:
        def update_ltk_library(self, **values: object) -> None:
            calls.append(values)

    library = tmp_path / "ltk"
    library.mkdir()
    (library / "library.json").write_text(
        json.dumps({"mods": [{"id": "one"}], "profiles": [], "activeProfileId": None}),
        encoding="utf-8",
    )

    app_main._refresh_ltk_library_summary(
        cast(Any, Reconciler()),
        cast(Any, Tray()),
        lambda: True,
        logging.getLogger("test.ltk_library.startup"),
        with_drift=False,
    )

    assert inspections == []
    assert calls == [{"installed": True, "in_library": 1, "enabled": 0}]
