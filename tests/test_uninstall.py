"""Tests for removing everything the application caused to exist."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from league_skin_manager import uninstall as uninstall_module
from league_skin_manager.uninstall import UninstallReport, uninstall


@pytest.fixture
def stubbed(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace every OS-touching call with a recorder."""

    calls: dict[str, Any] = {"startup": [], "shortcut": 0, "launched": [], "ltk_data": []}

    monkeypatch.setattr(
        uninstall_module.windows,
        "set_startup_enabled",
        lambda executable, enabled: calls["startup"].append((executable, enabled)) or True,
    )
    monkeypatch.setattr(
        uninstall_module.windows,
        "remove_start_menu_shortcut",
        lambda: calls.__setitem__("shortcut", calls["shortcut"] + 1) or True,
    )
    monkeypatch.setattr(
        uninstall_module.windows,
        "launch_detached",
        lambda path, *a: calls["launched"].append(path) or True,
    )
    monkeypatch.setattr(uninstall_module.ltk, "uninstaller", lambda: Path("uninstall.exe"))

    def remove_data(path: Path | None = None) -> bool:
        calls["ltk_data"].append(path)
        if path is not None and path.is_dir():
            import shutil

            shutil.rmtree(path)
            return True
        return False

    monkeypatch.setattr(uninstall_module.ltk, "remove_data", remove_data)
    return calls


def make_tree(root: Path, name: str) -> Path:
    target = root / name
    (target / "logs").mkdir(parents=True)
    (target / "logs" / "app.log").write_text("log", encoding="utf-8")
    (target / "settings.json").write_text("{}", encoding="utf-8")
    return target


def test_application_data_is_removed(tmp_path: Path, stubbed: dict[str, Any]) -> None:
    data = make_tree(tmp_path, "app")
    report = uninstall(
        data_dir=data, executable=tmp_path / "app.exe", remove_ltk=False, close_logging=False
    )
    assert report.app_data is True
    assert not data.exists()


def test_the_startup_entry_and_shortcut_are_removed(
    tmp_path: Path, stubbed: dict[str, Any]
) -> None:
    data = make_tree(tmp_path, "app")
    executable = tmp_path / "app.exe"
    report = uninstall(data_dir=data, executable=executable, remove_ltk=False, close_logging=False)
    assert stubbed["startup"] == [(executable, False)]
    assert stubbed["shortcut"] == 1
    assert report.startup is True
    assert report.shortcut is True


def test_ltk_is_untouched_when_we_did_not_install_it(
    tmp_path: Path, stubbed: dict[str, Any]
) -> None:
    """An LTK that was already present is never removed, ever."""

    data = make_tree(tmp_path, "app")
    ltk_data = make_tree(tmp_path, "ltk")
    report = uninstall(
        data_dir=data,
        executable=tmp_path / "app.exe",
        remove_ltk=False,
        ltk_data_dir=ltk_data,
        close_logging=False,
    )
    assert ltk_data.exists()
    assert stubbed["launched"] == []
    assert stubbed["ltk_data"] == []
    assert report.ltk_app is False
    assert report.ltk_data is False


def test_ltk_is_removed_when_we_installed_it(tmp_path: Path, stubbed: dict[str, Any]) -> None:
    data = make_tree(tmp_path, "app")
    ltk_data = make_tree(tmp_path, "ltk")
    report = uninstall(
        data_dir=data,
        executable=tmp_path / "app.exe",
        remove_ltk=True,
        ltk_data_dir=ltk_data,
        close_logging=False,
    )
    assert not ltk_data.exists()
    assert stubbed["launched"] == [Path("uninstall.exe")]
    assert report.ltk_app is True
    assert report.ltk_data is True


def test_a_missing_data_directory_is_not_a_failure(tmp_path: Path, stubbed: dict[str, Any]) -> None:
    report = uninstall(
        data_dir=tmp_path / "absent",
        executable=tmp_path / "app.exe",
        remove_ltk=False,
        close_logging=False,
    )
    assert report.app_data is False
    assert report.succeeded


def test_a_missing_ltk_uninstaller_is_recorded_as_a_failure(
    tmp_path: Path, stubbed: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(uninstall_module.ltk, "uninstaller", lambda: None)
    report = uninstall(
        data_dir=make_tree(tmp_path, "app"),
        executable=tmp_path / "app.exe",
        remove_ltk=True,
        ltk_data_dir=tmp_path / "absent-ltk",
        close_logging=False,
    )
    assert not report.succeeded
    assert any("uninstaller" in failure for failure in report.failures)


def test_logging_is_closed_before_the_directory_is_deleted(tmp_path: Path) -> None:
    """Windows refuses to delete a directory holding an open log handle."""

    import logging

    data = make_tree(tmp_path, "app")
    logger = logging.getLogger("league_skin_manager")
    handler = logging.FileHandler(data / "logs" / "held.log", encoding="utf-8")
    logger.addHandler(handler)
    logger.warning("holding the file open")

    uninstall_module._shutdown_logging()

    assert handler not in logger.handlers
    import shutil

    shutil.rmtree(data)  # must not raise
    assert not data.exists()


# --- reporting -------------------------------------------------------------


def test_the_summary_lists_what_went() -> None:
    report = UninstallReport(app_data=True, ltk_data=True, startup=True)
    summary = report.summary()
    assert "application data" in summary
    assert "LTK skin library" in summary
    assert "startup entry" in summary


def test_an_empty_summary_says_so() -> None:
    assert UninstallReport().summary() == "Nothing needed removing."


def test_failures_mark_the_report_unsuccessful() -> None:
    assert UninstallReport().succeeded is True
    assert UninstallReport(failures=("something",)).succeeded is False
