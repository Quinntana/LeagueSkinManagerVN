from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pytest

from league_skin_manager.controller import AppState
from league_skin_manager.tray import MENU_TEXT_LIMIT, TITLE_LIMIT, TrayApplication

SEPARATOR = object()


@dataclass
class FakeMenuItem:
    text: str
    action: object | None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMenu:
    items: tuple[object, ...]


class FakeMenuFactory:
    """Callable menu factory that also carries pystray's SEPARATOR sentinel."""

    SEPARATOR = SEPARATOR

    def __call__(self, *items: object) -> FakeMenu:
        return FakeMenu(tuple(items))


class FakeIcon:
    def __init__(self, name: str, image: object, title: str, menu: FakeMenu) -> None:
        self.name = name
        self.icon = image
        self.title = title
        self.menu: object = menu
        self.visible = False
        self.stop_calls = 0
        self.menu_updates = 0
        self.notifications: list[tuple[str | None, str]] = []

    def run(self, setup: Callable[[FakeIcon], None] | None = None) -> None:
        if setup is None:
            self.visible = True
        else:
            setup(self)

    def run_detached(self, setup: Callable[[FakeIcon], None] | None = None) -> None:
        self.run(setup=setup)

    def stop(self) -> None:
        self.stop_calls += 1
        self.visible = False

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((title, message))

    def update_menu(self) -> None:
        self.menu_updates += 1


class FakeBackend:
    def __init__(self) -> None:
        self.icon: FakeIcon | None = None
        self.Menu = FakeMenuFactory()

    def Icon(self, name: str, image: object, title: str, menu: object) -> FakeIcon:
        assert isinstance(menu, FakeMenu)
        self.icon = FakeIcon(name, image, title, menu)
        return self.icon

    def MenuItem(self, text: str, action: object | None, **options: Any) -> FakeMenuItem:
        return FakeMenuItem(text, action, options)


def make_tray(backend: FakeBackend, **overrides: Any) -> TrayApplication:
    callbacks: dict[str, Any] = {
        "on_start": lambda: None,
        "on_sync": lambda: None,
        "on_open_ltk": lambda: None,
        "on_open_cooldowns": lambda: None,
        "on_open_ltk_skins": lambda: None,
        "on_open_cslol_skins": lambda: None,
        "on_open_data": lambda: None,
        "on_open_log": lambda: None,
        "on_rebuild_library": lambda: None,
        "on_remove_ltk_skins": lambda: None,
        "on_uninstall": lambda: None,
        "on_exit": lambda: None,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: None,
        "runtime_label": "Installed v9.9.9",
        "uninstall_available": True,
        "startup_available": True,
        "backend": backend,
        "image_factory": lambda state: f"image:{state.name}",
        "app_name": "TestApp",
    }
    callbacks.update(overrides)
    return TrayApplication(**callbacks)


def rows(tray: TrayApplication) -> tuple[object, ...]:
    menu = tray.native_icon.menu
    assert isinstance(menu, FakeMenu)
    return menu.items


def labels(items: Iterable[object]) -> list[str]:
    return [item.text if isinstance(item, FakeMenuItem) else "---" for item in items]


def find_item(items: Iterable[object], text: str) -> FakeMenuItem:
    matches = [item for item in items if isinstance(item, FakeMenuItem) and item.text == text]
    assert len(matches) == 1, f"expected exactly one menu item named {text!r}"
    return matches[0]


def find_prefixed(items: Iterable[object], prefix: str) -> FakeMenuItem:
    matches = [
        item for item in items if isinstance(item, FakeMenuItem) and item.text.startswith(prefix)
    ]
    assert len(matches) == 1, f"expected exactly one item starting with {prefix!r}"
    return matches[0]


def submenu(tray: TrayApplication, text: str) -> tuple[object, ...]:
    action = find_item(rows(tray), text).action
    assert isinstance(action, FakeMenu)
    return action.items


def fake_icon(tray: TrayApplication) -> FakeIcon:
    icon = tray.native_icon
    assert isinstance(icon, FakeIcon)
    return icon


def click(tray: TrayApplication, item: FakeMenuItem) -> None:
    assert callable(item.action)
    item.action(tray.native_icon, item)


def test_tray_is_visible_before_startup_and_has_the_expected_menu() -> None:
    backend = FakeBackend()
    visibility: list[bool] = []

    def record() -> None:
        assert backend.icon is not None
        visibility.append(backend.icon.visible)

    tray = make_tray(backend, on_start=record)

    tray.run()

    assert visibility == [True]
    assert fake_icon(tray).visible is True
    assert labels(rows(tray)) == [
        "Starting - reading local catalog",
        "LTK: checking the latest official release",
        "---",
        "Open LTK Manager",
        "Sync skins now",
        "Enemy cooldown timers...",
        "---",
        "Folders",
        "Advanced",
        "---",
        "Start with Windows",
        "Exit",
    ]
    assert labels(submenu(tray, "Folders")) == [
        "Skins in LTK",
        "Skins in CSLOL",
        "App data",
        "Diagnostics log",
    ]
    assert labels(submenu(tray, "Advanced")) == [
        "Rebuild LTK library now",
        "---",
        "Remove all skins from LTK...",
        "Uninstall TestApp...",
    ]
    assert find_item(rows(tray), "Open LTK Manager").options["default"] is True
    for prefix in ("Starting", "LTK:"):
        assert find_prefixed(rows(tray), prefix).options["enabled"] is False


def test_the_menu_carries_no_removed_vocabulary() -> None:
    tray = make_tray(FakeBackend())
    tray.update_ltk_library(installed=True, in_library=3, enabled=1)

    text = " ".join(
        labels(rows(tray)) + labels(submenu(tray, "Folders")) + labels(submenu(tray, "Advanced"))
    ).casefold()

    for gone in ("port", "handoff", "browse", "search", "cslol manager", "migrat"):
        assert gone not in text


def test_status_rows_report_sync_state_counts_and_patch() -> None:
    tray = make_tray(FakeBackend())

    tray.update_status(AppState.READY, "Ready - 1907 skins")
    tray.update_library(1907, "25.14")

    assert find_prefixed(rows(tray), "Ready").text == "Ready - 1,907 skins - patch 25.14"
    assert tray.state is AppState.READY
    assert fake_icon(tray).icon == "image:READY"


def test_an_error_state_shows_the_diagnostic_detail() -> None:
    tray = make_tray(FakeBackend())
    tray.update_library(10, "25.14")

    tray.update_status(AppState.ERROR, "Sync failed: disk full")

    assert find_prefixed(rows(tray), "Error:").text == "Error: Sync failed: disk full"


@pytest.mark.parametrize(
    ("state", "word"),
    [
        (AppState.OFFLINE_READY, "Offline"),
        (AppState.SYNCING, "Syncing"),
        (AppState.STOPPING, "Stopping"),
    ],
)
def test_each_state_has_a_short_word(state: AppState, word: str) -> None:
    tray = make_tray(FakeBackend())
    tray.update_library(5, None)

    tray.update_status(state, "detail")

    assert find_prefixed(rows(tray), word).text == f"{word} - 5 skins"


def test_ltk_row_reports_library_counts_and_enabled_skins() -> None:
    tray = make_tray(FakeBackend())

    tray.update_ltk_library(installed=True, in_library=1922, enabled=0, expected=1922, pending=0)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: 1,922 skins - 0 enabled"


def test_ltk_row_reports_drift_when_a_rebuild_is_needed() -> None:
    tray = make_tray(FakeBackend())

    tray.update_ltk_library(installed=True, in_library=1900, enabled=2, expected=1927, pending=27)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: 1,900 of 1,927 skins - 27 to rebuild"
    assert "rebuild pending" in fake_icon(tray).title


def test_ltk_row_reports_a_missing_install_and_offers_to_install() -> None:
    tray = make_tray(FakeBackend())

    tray.update_ltk_library(installed=False)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: not installed"
    assert find_item(rows(tray), "Install LTK Manager...").options["default"] is True


def test_ltk_row_reports_an_unreadable_library() -> None:
    tray = make_tray(FakeBackend())
    tray.update_ltk_status("idle", rebuild_active=False)

    tray.update_ltk_library(installed=True)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: idle"


def test_active_rebuild_activity_replaces_the_library_summary() -> None:
    tray = make_tray(FakeBackend())
    tray.update_ltk_library(installed=True, in_library=5, enabled=0)

    tray.update_ltk_status("queueing 12/1927 - Ahri", rebuild_active=True)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: queueing 12/1927 - Ahri"

    tray.update_ltk_status("done", rebuild_active=False)

    assert find_prefixed(rows(tray), "LTK:").text == "LTK: 5 skins - 0 enabled"


@pytest.mark.parametrize("value", [-1, True])
def test_ltk_library_counts_must_be_non_negative_integers(value: Any) -> None:
    tray = make_tray(FakeBackend())

    with pytest.raises(ValueError, match="non-negative"):
        tray.update_ltk_library(installed=True, in_library=value)


def test_negative_skin_counts_are_rejected() -> None:
    tray = make_tray(FakeBackend())

    with pytest.raises(ValueError, match="cannot be negative"):
        tray.update_library(-1, None)


def test_blank_runtime_label_is_rejected() -> None:
    with pytest.raises(ValueError, match="runtime_label"):
        make_tray(FakeBackend(), runtime_label="  ")


def test_tooltip_and_menu_rows_stay_within_platform_bounds() -> None:
    tray = make_tray(FakeBackend(), runtime_label="Installed v9.9.9")

    tray.update_status(AppState.ERROR, "x" * 400)
    tray.update_ltk_status("y" * 400, rebuild_active=True)
    tray.update_library(1234567, "z" * 200)

    assert len(fake_icon(tray).title) <= TITLE_LIMIT
    for item in rows(tray):
        if isinstance(item, FakeMenuItem):
            assert len(item.text) <= MENU_TEXT_LIMIT


def test_sync_is_disabled_while_busy() -> None:
    tray = make_tray(FakeBackend())

    for state in (AppState.STARTING, AppState.SYNCING, AppState.STOPPING):
        tray.update_status(state, "busy")
        assert find_item(rows(tray), "Sync skins now").options["enabled"] is False

    tray.update_status(AppState.READY, "Ready")
    assert find_item(rows(tray), "Sync skins now").options["enabled"] is True


def test_portable_runtime_disables_install_owned_actions() -> None:
    tray = make_tray(
        FakeBackend(),
        runtime_label="Portable v9.9.9",
        uninstall_available=False,
        startup_available=False,
    )

    assert find_item(submenu(tray, "Advanced"), "Uninstall TestApp...").options["enabled"] is False
    assert find_item(rows(tray), "Start with Windows").options["enabled"] is False
    assert "Portable v9.9.9" in fake_icon(tray).title


def test_each_action_dispatches_only_its_own_callback() -> None:
    calls: list[str] = []
    tray = make_tray(
        FakeBackend(),
        on_sync=lambda: calls.append("sync"),
        on_open_ltk=lambda: calls.append("ltk"),
        on_open_cooldowns=lambda: calls.append("cooldowns"),
        on_open_ltk_skins=lambda: calls.append("ltk-skins"),
        on_open_cslol_skins=lambda: calls.append("cslol-skins"),
        on_open_data=lambda: calls.append("data"),
        on_open_log=lambda: calls.append("log"),
        on_rebuild_library=lambda: calls.append("rebuild"),
        on_remove_ltk_skins=lambda: calls.append("remove"),
    )
    tray.update_status(AppState.READY, "Ready")

    actions = [
        (rows(tray), "Open LTK Manager", "ltk"),
        (rows(tray), "Sync skins now", "sync"),
        (rows(tray), "Enemy cooldown timers...", "cooldowns"),
        (submenu(tray, "Folders"), "Skins in LTK", "ltk-skins"),
        (submenu(tray, "Folders"), "Skins in CSLOL", "cslol-skins"),
        (submenu(tray, "Folders"), "App data", "data"),
        (submenu(tray, "Folders"), "Diagnostics log", "log"),
        (submenu(tray, "Advanced"), "Rebuild LTK library now", "rebuild"),
        (submenu(tray, "Advanced"), "Remove all skins from LTK...", "remove"),
    ]
    for items, text, expected in actions:
        calls.clear()
        click(tray, find_item(items, text))
        assert calls == [expected], f"{text} dispatched {calls}"


def test_callback_errors_are_contained_and_reported() -> None:
    def explode() -> None:
        raise RuntimeError("boom")

    tray = make_tray(FakeBackend(), on_sync=explode)
    tray.update_status(AppState.READY, "Ready")

    click(tray, find_item(rows(tray), "Sync skins now"))

    title, message = fake_icon(tray).notifications[-1]
    assert title == "TestApp"
    assert "boom" in message


def test_startup_toggle_refreshes_checked_state() -> None:
    enabled = {"value": False}
    tray = make_tray(
        FakeBackend(),
        startup_enabled=lambda: enabled["value"],
        set_startup_enabled=lambda desired: enabled.__setitem__("value", desired),
    )

    item = find_item(rows(tray), "Start with Windows")
    assert item.options["checked"](item) is False
    click(tray, item)

    assert enabled["value"] is True
    refreshed = find_item(rows(tray), "Start with Windows")
    assert refreshed.options["checked"](refreshed) is True


def test_a_refused_startup_toggle_notifies_without_changing_state() -> None:
    tray = make_tray(
        FakeBackend(),
        startup_enabled=lambda: False,
        set_startup_enabled=lambda _desired: False,
    )

    click(tray, find_item(rows(tray), "Start with Windows"))

    title, message = fake_icon(tray).notifications[-1]
    assert title == "Start with Windows"
    assert "could not be updated" in message


def test_a_timed_out_shutdown_keeps_the_tray_active_and_allows_retry() -> None:
    results = iter((False, True))
    tray = make_tray(FakeBackend(), on_exit=lambda: next(results))

    click(tray, find_item(rows(tray), "Exit"))

    assert fake_icon(tray).stop_calls == 0
    assert "try Exit again" in fake_icon(tray).notifications[-1][1]

    click(tray, find_item(rows(tray), "Exit"))

    assert fake_icon(tray).stop_calls == 1


def test_a_shutdown_callback_error_does_not_stop_the_tray() -> None:
    def explode() -> bool:
        raise RuntimeError("shutdown failed")

    tray = make_tray(FakeBackend(), on_exit=explode)

    click(tray, find_item(rows(tray), "Exit"))

    assert fake_icon(tray).stop_calls == 0
    assert "shutdown failed" in fake_icon(tray).notifications[-1][1]


def test_uninstall_uses_the_same_bounded_shutdown_path() -> None:
    calls: list[str] = []
    tray = make_tray(
        FakeBackend(),
        on_uninstall=lambda: calls.append("uninstall") or True,
    )

    click(tray, find_item(submenu(tray, "Advanced"), "Uninstall TestApp..."))

    assert calls == ["uninstall"]
    assert fake_icon(tray).stop_calls == 1


def test_stop_is_idempotent() -> None:
    tray = make_tray(FakeBackend())

    tray.stop()
    tray.stop()

    assert fake_icon(tray).stop_calls == 1
