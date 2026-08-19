"""Tests for the composition root's sequencing decisions.

Wiring is not tested here; the decisions are. There are only a few, and each
one is a rule that was argued for rather than fallen into.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from league_skin_manager import app as app_module
from league_skin_manager.app import BLOCKED_MESSAGE, App
from league_skin_manager.config import AppPaths
from league_skin_manager.settings import Settings
from league_skin_manager.sync import SyncOutcome, SyncResult


def fake_sync(outcome: str, commit: str, seeded: int = 0) -> Any:
    """Model synchronize's real contract: it derives from the settings given.

    Returning a bare Settings would silently discard unrelated fields, which
    is exactly the bug this shape prevents.
    """

    def run(*, settings: Settings, **_k: Any) -> Any:
        if outcome == SyncOutcome.UP_TO_DATE:
            return SyncResult(outcome, commit=commit, seeded=settings.skins), settings
        updated = settings.with_sync(
            commit=commit, patch="16.15.1", skins=seeded, synced_at="2026-08-19T08:00:00Z"
        )
        return SyncResult(outcome, commit=commit, seeded=seeded), updated

    return run


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Neutralise every adapter so only the sequencing runs."""

    calls: dict[str, Any] = {
        "installed": [],
        "launched": [],
        "synced": 0,
        "settings_applied": 0,
        "shortcut": 0,
    }

    monkeypatch.setattr(app_module.windows, "startup_enabled", lambda _e: False)
    monkeypatch.setattr(
        app_module.windows,
        "create_start_menu_shortcut",
        lambda _e: calls.__setitem__("shortcut", calls["shortcut"] + 1) or True,
    )
    monkeypatch.setattr(
        app_module.windows.ProcessLookup, "is_any_running", staticmethod(lambda _n: False)
    )
    monkeypatch.setattr(
        app_module.ltk,
        "apply_settings",
        lambda _d=None: (
            calls.__setitem__("settings_applied", calls["settings_applied"] + 1) or True
        ),
    )
    monkeypatch.setattr(app_module.ltk, "resolve_storage_dir", lambda _d=None: tmp_path / "ltk")
    monkeypatch.setattr(
        app_module.ltk, "launch", lambda exe, **_k: calls["launched"].append(exe) or True
    )
    monkeypatch.setattr(app_module.ltk, "ReleaseClient", lambda *a, **k: _FakeClient())
    monkeypatch.setattr(
        app_module.ltk,
        "install",
        lambda *a, **k: calls["installed"].append(True) or Path("installer.exe"),
    )
    return calls


class _FakeClient:
    def close(self) -> None:
        return None


class _FakeTray:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.state = type("S", (), {})()
        self.notifications: list[tuple[str, str]] = []
        self.refreshes: list[dict[str, Any]] = []

    def refresh(self, **changes: Any) -> None:
        self.refreshes.append(changes)

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))

    def run(self) -> None:
        return None

    def stop(self) -> None:
        return None


def make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> App:
    monkeypatch.setattr(app_module, "Tray", _FakeTray)
    monkeypatch.setattr(app_module, "GameWatcher", lambda *a, **k: _FakeWatcher())
    paths = AppPaths.discover(appdata=tmp_path)
    paths.ensure()
    return App(paths=paths, executable=tmp_path / "app.exe", settings=settings)


class _FakeWatcher:
    match_active = False

    def start(self) -> bool:
        return True

    def stop(self, _timeout: float = 5.0) -> bool:
        return True


# --- ownership ------------------------------------------------------------


def test_a_pre_existing_ltk_blocks_syncing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    """An LTK we did not install is never touched, only refused."""

    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=False))

    app._startup()

    assert app._blocked == BLOCKED_MESSAGE
    assert wired["installed"] == []
    assert wired["synced"] == 0


def test_an_absent_ltk_is_installed_and_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: None)
    monkeypatch.setattr(app_module, "synchronize", fake_sync(SyncOutcome.UPDATED, "abc", 5))
    app = make_app(tmp_path, monkeypatch, Settings())

    app._startup()

    assert wired["installed"] == [True]
    assert app.settings.ltk_installed_by_app is True, "ownership must be recorded at install time"


def test_our_own_ltk_is_not_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))
    monkeypatch.setattr(app_module, "synchronize", fake_sync(SyncOutcome.UP_TO_DATE, "abc"))
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=True, commit="abc"))

    app._startup()

    assert app._blocked is None


# --- the advertise launch -------------------------------------------------


def test_ltk_opens_once_after_the_first_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    """The one moment a new user is shown that it worked."""

    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))
    monkeypatch.setattr(app_module, "synchronize", fake_sync(SyncOutcome.UPDATED, "abc", 9))
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=True))

    app._startup()

    assert wired["launched"] == [Path("C:/LTK/ltk-manager.exe")]


def test_ltk_is_not_opened_on_later_syncs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))
    monkeypatch.setattr(app_module, "synchronize", fake_sync(SyncOutcome.UPDATED, "def"))
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=True, commit="abc"))

    app._startup()

    assert wired["launched"] == [], "the advertise launch is first-sync only"


def test_ltk_settings_are_applied_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))
    monkeypatch.setattr(app_module, "synchronize", fake_sync(SyncOutcome.UP_TO_DATE, "abc"))
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=True, commit="abc"))

    app._startup()

    assert wired["settings_applied"] == 1


# --- cooldown suppression -------------------------------------------------


def test_closing_the_board_suppresses_it_for_that_match_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    app = make_app(tmp_path, monkeypatch, Settings(cooldown_auto_run=True))
    app.watcher.match_active = True  # type: ignore[attr-defined]

    app._on_cooldowns_closed()
    assert app._suppressed_for_match is True

    # A new match clears it.
    app.watcher.match_active = False  # type: ignore[attr-defined]
    app._on_game_change(False)
    assert app._suppressed_for_match is False


def test_closing_the_board_outside_a_match_does_not_suppress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    app = make_app(tmp_path, monkeypatch, Settings())
    app.watcher.match_active = False  # type: ignore[attr-defined]
    app._on_cooldowns_closed()
    assert app._suppressed_for_match is False


def test_a_failed_startup_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wired: dict[str, Any]
) -> None:
    """The worker thread is the error boundary."""

    monkeypatch.setattr(app_module.ltk, "locate", lambda *a: Path("C:/LTK/ltk-manager.exe"))

    def explode(**_k: Any) -> Any:
        raise RuntimeError("github is down")

    monkeypatch.setattr(app_module, "synchronize", explode)
    app = make_app(tmp_path, monkeypatch, Settings(ltk_installed_by_app=True))

    app._startup()  # must not raise

    assert any("Startup failed" in title for title, _ in app.tray.notifications)
