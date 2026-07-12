from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, cast

import league_skin_manager.main as app_main
from league_skin_manager.config import AppPaths
from league_skin_manager.controller import AppController


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


def test_second_instance_exits_without_launching_manager(
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

    class Launcher:
        def __init__(self, _logger: logging.Logger) -> None:
            return None

        def launch(self, executable: Path) -> bool:
            launches.append(executable)
            return True

    monkeypatch.setattr(AppPaths, "discover", staticmethod(lambda: Paths()))
    monkeypatch.setattr(app_main, "configure_logging", lambda _path: logging.getLogger("test"))
    monkeypatch.setattr(app_main, "SingleInstanceMutex", Mutex)
    monkeypatch.setattr(app_main, "ProcessLauncher", Launcher)
    monkeypatch.setattr(sys, "platform", "win32")

    assert app_main.run() == 2
    assert launches == []


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
