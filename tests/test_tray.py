from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import pytest

from league_skin_manager.controller import AppState
from league_skin_manager.tray import TrayApplication


@dataclass
class FakeMenuItem:
    text: str
    action: object | None
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeMenu:
    items: tuple[FakeMenuItem, ...]


class FakeIcon:
    def __init__(
        self,
        name: str,
        image: object,
        title: str,
        menu: FakeMenu,
    ) -> None:
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

    def Icon(
        self,
        name: str,
        image: object,
        title: str,
        menu: object,
    ) -> FakeIcon:
        assert isinstance(menu, FakeMenu)
        self.icon = FakeIcon(name, image, title, menu)
        return self.icon

    def Menu(self, *items: object) -> FakeMenu:
        assert all(isinstance(item, FakeMenuItem) for item in items)
        return FakeMenu(tuple(items))  # type: ignore[arg-type]

    def MenuItem(
        self,
        text: str,
        action: object | None,
        **options: Any,
    ) -> FakeMenuItem:
        return FakeMenuItem(text, action, options)


def make_tray(
    backend: FakeBackend,
    **overrides: Any,
) -> TrayApplication:
    callbacks: dict[str, Any] = {
        "on_start": lambda: None,
        "on_show": lambda: None,
        "on_sync": lambda: None,
        "on_start_manager": lambda: None,
        "on_open_cslol_skins": lambda: None,
        "on_copy_cslol_manager_path": lambda: None,
        "on_start_ltk": lambda: None,
        "on_open_ltk_install": lambda: None,
        "on_open_ltk_storage": lambda: None,
        "on_migrate_to_ltk": lambda: None,
        "on_cancel_ltk_migration": lambda: None,
        "on_reset_ltk_migration": lambda: None,
        "on_remove_ltk_skins": lambda: None,
        "on_open_data": lambda: None,
        "on_open_log": lambda: None,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: None,
        "on_uninstall": lambda: None,
        "on_exit": lambda: None,
        "runtime_label": "Installed v9.9.9",
        "uninstall_available": True,
        "startup_available": True,
        "backend": backend,
        "image_factory": lambda state: f"image:{state.name}",
        "app_name": "Test Skin Manager",
    }
    callbacks.update(overrides)
    return TrayApplication(**callbacks)


def menu_items(tray: TrayApplication) -> tuple[FakeMenuItem, ...]:
    menu = tray.native_icon.menu
    assert isinstance(menu, FakeMenu)
    return menu.items


def find_item(items: Iterable[FakeMenuItem], text: str) -> FakeMenuItem:
    matches = [item for item in items if item.text == text]
    assert len(matches) == 1, f"expected exactly one menu item named {text!r}"
    return matches[0]


def find_prefixed_item(
    items: Iterable[FakeMenuItem],
    prefix: str,
) -> FakeMenuItem:
    matches = [item for item in items if item.text.startswith(prefix)]
    assert len(matches) == 1, f"expected exactly one menu item starting with {prefix!r}"
    return matches[0]


def root_item(tray: TrayApplication, text: str) -> FakeMenuItem:
    return find_item(menu_items(tray), text)


def submenu_items(
    tray: TrayApplication,
    text: str,
) -> tuple[FakeMenuItem, ...]:
    action = root_item(tray, text).action
    assert isinstance(action, FakeMenu)
    return action.items


def fake_icon(tray: TrayApplication) -> FakeIcon:
    icon = tray.native_icon
    assert isinstance(icon, FakeIcon)
    return icon


def click(tray: TrayApplication, item: FakeMenuItem) -> None:
    assert callable(item.action)
    item.action(tray.native_icon, item)


def test_tray_is_visible_before_startup_callback_and_has_expected_menu() -> None:
    backend = FakeBackend()
    callback_visibility: list[bool] = []

    def record_visibility() -> None:
        assert backend.icon is not None
        callback_visibility.append(backend.icon.visible)

    tray = make_tray(backend, on_start=record_visibility)

    tray.run()

    assert callback_visibility == [True]
    assert fake_icon(tray).visible is True
    assert [item.text for item in menu_items(tray)] == [
        "Browse/search VN skin library...",
        "Sync: Starting",
        "Library: reading local catalog",
        "LTK: checking the latest official release",
        "LTK port: checking VN handoff state",
        "Runtime: Installed v9.9.9",
        "Sync VN skins now",
        "CSLOL Manager",
        "LTK Manager",
        "Maintenance",
        "Start with Windows",
        "Exit",
    ]
    assert [item.text for item in submenu_items(tray, "CSLOL Manager")] == [
        "Open CSLOL Manager",
        "Open installed skins folder",
        "Copy CSLOL Manager folder path",
    ]
    assert [item.text for item in submenu_items(tray, "LTK Manager")] == [
        "Open or install LTK Manager",
        "Open LTK application folder",
        "Open LTK skin storage folder",
        "Port CSLOL skins to LTK now...",
        "Cancel active LTK port",
    ]
    assert [item.text for item in submenu_items(tray, "Maintenance")] == [
        "Open LeagueSkinManagerVN data folder",
        "Open diagnostics log",
        "Reset LTK port history...",
        "Remove all LTK skins...",
        "Uninstall LeagueSkinManagerVN...",
    ]
    assert root_item(tray, "Browse/search VN skin library...").options["default"] is True
    for prefix in ("Sync:", "Library:", "LTK:", "LTK port:", "Runtime:"):
        assert find_prefixed_item(menu_items(tray), prefix).options["enabled"] is False


def test_status_catalog_ltk_and_runtime_labels_refresh_independently() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_status(AppState.READY, "Catalog is current")
    tray.update_library(1907, "25.14")
    tray.update_ltk_status("v1.11 installed")

    assert tray.state is AppState.READY
    assert tray.detail == "Catalog is current"
    assert tray.native_icon.title == ("Test Skin Manager | Catalog is current | 1,907 skins")
    assert tray.native_icon.icon == "image:READY"
    assert find_prefixed_item(menu_items(tray), "Sync:").text == "Sync: Catalog is current"
    assert find_prefixed_item(menu_items(tray), "Library:").text == (
        "Library: 1,907 VN skins | patch 25.14"
    )
    assert find_prefixed_item(menu_items(tray), "LTK:").text == "LTK: v1.11 installed"
    assert find_prefixed_item(menu_items(tray), "Runtime:").text == ("Runtime: Installed v9.9.9")
    assert fake_icon(tray).menu_updates == 3


def test_tooltip_and_dynamic_menu_labels_stay_within_platform_bounds() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_library(1907, "25.14")
    tray.update_status(AppState.READY, "download complete " * 20)
    tray.update_ltk_status("port status " * 20)

    assert len(tray.native_icon.title) <= 127
    assert tray.native_icon.title.endswith("...")
    assert len(find_prefixed_item(menu_items(tray), "Sync:").text) <= 96
    assert len(find_prefixed_item(menu_items(tray), "LTK:").text) <= 96


def test_ltk_port_status_advertises_only_an_explicit_manual_handoff() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_ltk_port_status(pending=12, total=1907)

    assert find_prefixed_item(menu_items(tray), "LTK port:").text == (
        "LTK port: 12 of 1,907 VN skins need manual port"
    )
    assert find_item(
        submenu_items(tray, "LTK Manager"),
        "Port CSLOL skins to LTK now (12 pending)...",
    )
    assert "LTK port recommended" in tray.native_icon.title

    tray.update_ltk_port_status(pending=0, total=1907)
    assert find_prefixed_item(menu_items(tray), "LTK port:").text == (
        "LTK port: all 1,907 current VN skins were queued"
    )
    assert find_item(
        submenu_items(tray, "LTK Manager"),
        "Port CSLOL skins to LTK now...",
    )
    assert "LTK port recommended" not in tray.native_icon.title

    tray.update_ltk_port_status(pending=0, total=0)
    assert find_prefixed_item(menu_items(tray), "LTK port:").text == (
        "LTK port: no VN-managed skins to port"
    )

    tray.update_ltk_port_status(pending=None, total=None, unavailable=True)
    assert find_prefixed_item(menu_items(tray), "LTK port:").text == (
        "LTK port: status unavailable; check diagnostics"
    )


@pytest.mark.parametrize(
    ("pending", "total", "unavailable"),
    [
        (None, 1, False),
        (1, None, False),
        (-1, 1, False),
        (2, 1, False),
        (True, 1, False),
        (0, 1, True),
    ],
)
def test_ltk_port_status_rejects_inconsistent_counts(
    pending: int | None,
    total: int | None,
    unavailable: bool,
) -> None:
    tray = make_tray(FakeBackend())

    with pytest.raises(ValueError):
        tray.update_ltk_port_status(
            pending=pending,
            total=total,
            unavailable=unavailable,
        )


def test_sync_and_cancel_actions_track_operational_state() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    sync = root_item(tray, "Sync VN skins now")
    cancel = find_item(submenu_items(tray, "LTK Manager"), "Cancel active LTK port")
    assert sync.options["enabled"] is False
    assert cancel.options["enabled"] is False

    tray.update_status(AppState.READY, "Ready")
    assert root_item(tray, "Sync VN skins now").options["enabled"] is True

    tray.update_status(AppState.SYNCING, "Downloading")
    assert root_item(tray, "Sync VN skins now").options["enabled"] is False

    tray.update_ltk_status("Porting 4 of 20", migration_active=True)
    assert (
        find_item(
            submenu_items(tray, "LTK Manager"),
            "Cancel active LTK port",
        ).options["enabled"]
        is True
    )

    tray.update_ltk_status("Port cancelled", migration_active=False)
    assert (
        find_item(
            submenu_items(tray, "LTK Manager"),
            "Cancel active LTK port",
        ).options["enabled"]
        is False
    )

    tray.update_status(AppState.STOPPING, "Stopping")
    assert root_item(tray, "Sync VN skins now").options["enabled"] is False


def test_portable_runtime_disables_install_owned_actions() -> None:
    backend = FakeBackend()
    tray = make_tray(
        backend,
        runtime_label="Portable v2.7.0",
        uninstall_available=False,
        startup_available=False,
    )

    assert root_item(tray, "Runtime: Portable v2.7.0").options["enabled"] is False
    assert root_item(tray, "Start with Windows").options["enabled"] is False
    assert (
        find_item(
            submenu_items(tray, "Maintenance"),
            "Uninstall LeagueSkinManagerVN...",
        ).options["enabled"]
        is False
    )


def test_each_non_shutdown_menu_action_dispatches_only_its_callback() -> None:
    backend = FakeBackend()
    calls: list[str] = []
    tray = make_tray(
        backend,
        on_show=lambda: calls.append("show"),
        on_sync=lambda: calls.append("sync"),
        on_start_manager=lambda: calls.append("manager"),
        on_open_cslol_skins=lambda: calls.append("cslol-skins"),
        on_copy_cslol_manager_path=lambda: calls.append("copy-cslol-root"),
        on_start_ltk=lambda: calls.append("ltk"),
        on_open_ltk_install=lambda: calls.append("ltk-app-folder"),
        on_open_ltk_storage=lambda: calls.append("ltk-storage"),
        on_migrate_to_ltk=lambda: calls.append("port"),
        on_cancel_ltk_migration=lambda: calls.append("cancel-port"),
        on_open_data=lambda: calls.append("data"),
        on_open_log=lambda: calls.append("log"),
        on_reset_ltk_migration=lambda: calls.append("reset-port"),
        on_remove_ltk_skins=lambda: calls.append("clean-ltk"),
    )
    tray.update_status(AppState.READY, "Ready")
    tray.update_ltk_status("Porting", migration_active=True)
    actions = [
        (menu_items(tray), "Browse/search VN skin library...", "show"),
        (menu_items(tray), "Sync VN skins now", "sync"),
        (submenu_items(tray, "CSLOL Manager"), "Open CSLOL Manager", "manager"),
        (
            submenu_items(tray, "CSLOL Manager"),
            "Open installed skins folder",
            "cslol-skins",
        ),
        (
            submenu_items(tray, "CSLOL Manager"),
            "Copy CSLOL Manager folder path",
            "copy-cslol-root",
        ),
        (submenu_items(tray, "LTK Manager"), "Open or install LTK Manager", "ltk"),
        (
            submenu_items(tray, "LTK Manager"),
            "Open LTK application folder",
            "ltk-app-folder",
        ),
        (
            submenu_items(tray, "LTK Manager"),
            "Open LTK skin storage folder",
            "ltk-storage",
        ),
        (
            submenu_items(tray, "LTK Manager"),
            "Port CSLOL skins to LTK now...",
            "port",
        ),
        (
            submenu_items(tray, "LTK Manager"),
            "Cancel active LTK port",
            "cancel-port",
        ),
        (
            submenu_items(tray, "Maintenance"),
            "Open LeagueSkinManagerVN data folder",
            "data",
        ),
        (submenu_items(tray, "Maintenance"), "Open diagnostics log", "log"),
        (
            submenu_items(tray, "Maintenance"),
            "Reset LTK port history...",
            "reset-port",
        ),
        (
            submenu_items(tray, "Maintenance"),
            "Remove all LTK skins...",
            "clean-ltk",
        ),
    ]

    expected_calls: list[str] = []
    for items, label, expected_call in actions:
        click(tray, find_item(items, label))
        expected_calls.append(expected_call)
        assert calls == expected_calls


def test_callback_errors_are_contained_and_reported() -> None:
    backend = FakeBackend()

    def fail() -> None:
        raise RuntimeError("sync exploded")

    tray = make_tray(backend, on_sync=fail)

    click(tray, root_item(tray, "Sync VN skins now"))

    assert fake_icon(tray).notifications == [
        (
            "LeagueSkinManagerVN",
            "Could not complete skin sync: sync exploded",
        )
    ]


def test_startup_toggle_refreshes_checked_state() -> None:
    backend = FakeBackend()
    calls: list[bool] = []
    startup = {"enabled": False}

    def set_startup(enabled: bool) -> bool:
        startup["enabled"] = enabled
        calls.append(enabled)
        return True

    tray = make_tray(
        backend,
        startup_enabled=lambda: startup["enabled"],
        set_startup_enabled=set_startup,
    )

    click(tray, root_item(tray, "Start with Windows"))

    refreshed = root_item(tray, "Start with Windows")
    checked = refreshed.options["checked"]
    assert callable(checked)
    assert checked(refreshed) is True
    assert calls == [True]


def test_failed_startup_toggle_keeps_existing_state_and_notifies() -> None:
    backend = FakeBackend()
    tray = make_tray(
        backend,
        startup_enabled=lambda: False,
        set_startup_enabled=lambda _enabled: False,
    )

    click(tray, root_item(tray, "Start with Windows"))

    assert fake_icon(tray).notifications == [
        ("Start with Windows", "The startup setting could not be updated.")
    ]


def test_timed_out_shutdown_keeps_tray_active_and_allows_retry() -> None:
    backend = FakeBackend()
    outcomes = iter((False, True))
    exit_calls = 0

    def exit_app() -> bool:
        nonlocal exit_calls
        exit_calls += 1
        return next(outcomes)

    tray = make_tray(backend, on_exit=exit_app)

    click(tray, root_item(tray, "Exit"))
    assert exit_calls == 1
    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications == [
        (
            "Action not completed",
            "Background work is still stopping. The app remains active; try Exit again.",
        )
    ]

    click(tray, root_item(tray, "Exit"))
    assert exit_calls == 2
    assert fake_icon(tray).stop_calls == 1


def test_shutdown_callback_error_does_not_stop_tray() -> None:
    backend = FakeBackend()

    def fail_exit() -> bool:
        raise RuntimeError("worker state unavailable")

    tray = make_tray(backend, on_exit=fail_exit)

    click(tray, root_item(tray, "Exit"))

    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications == [
        (
            "LeagueSkinManagerVN",
            "Could not complete application shutdown: worker state unavailable",
        )
    ]


def test_tray_uninstall_uses_the_same_bounded_shutdown_path() -> None:
    backend = FakeBackend()
    calls: list[str] = []

    def uninstall() -> bool:
        calls.append("uninstall")
        return True

    tray = make_tray(backend, on_uninstall=uninstall)
    uninstall_item = find_item(
        submenu_items(tray, "Maintenance"),
        "Uninstall LeagueSkinManagerVN...",
    )

    click(tray, uninstall_item)
    click(tray, uninstall_item)

    assert calls == ["uninstall"]
    assert fake_icon(tray).stop_calls == 1
