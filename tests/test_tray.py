from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

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
        "on_start_ltk": lambda: None,
        "on_open_ltk_install": lambda: None,
        "on_open_ltk_storage": lambda: None,
        "on_migrate_to_ltk": lambda: None,
        "on_remove_ltk_skins": lambda: None,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: None,
        "on_uninstall": lambda: None,
        "on_exit": lambda: None,
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


def fake_icon(tray: TrayApplication) -> FakeIcon:
    icon = tray.native_icon
    assert isinstance(icon, FakeIcon)
    return icon


def click(tray: TrayApplication, item: FakeMenuItem) -> None:
    assert callable(item.action)
    item.action(tray.native_icon, item)


def submenu(item: FakeMenuItem) -> tuple[FakeMenuItem, ...]:
    assert isinstance(item.action, FakeMenu)
    return item.action.items


def test_tray_is_visible_before_startup_callback_runs() -> None:
    backend = FakeBackend()
    callback_visibility: list[bool] = []
    ports: list[str] = []

    def record_visibility() -> None:
        assert backend.icon is not None
        callback_visibility.append(backend.icon.visible)

    tray = make_tray(
        backend,
        on_start=record_visibility,
        on_migrate_to_ltk=lambda: ports.append("port"),
    )

    tray.run()

    assert callback_visibility == [True]
    assert ports == []
    assert fake_icon(tray).visible is True
    items = menu_items(tray)
    assert [item.text for item in items] == [
        "Open LeagueSkinManagerVN",
        "Status: Starting",
        "Sync VN skins now",
        "CSLOL Manager",
        "LTK Manager",
        "Start with Windows",
        "Uninstall LeagueSkinManagerVN...",
        "Exit",
    ]
    assert [item.text for item in submenu(items[3])] == [
        "Open CSLOL Manager",
        "Open installed skins folder",
    ]
    assert [item.text for item in submenu(items[4])] == [
        "Open or install LTK Manager",
        "Open LTK application folder",
        "Open LTK skin storage folder",
        "Port CSLOL skins to LTK now...",
        "Remove all LTK skins...",
    ]
    assert items[0].options["default"] is True
    assert items[1].options["enabled"] is False


def test_status_sink_updates_title_icon_and_menu() -> None:
    backend = FakeBackend()
    tray = make_tray(backend)

    tray.update_status(AppState.SYNCING, "Downloading 4 of 20")

    assert tray.state is AppState.SYNCING
    assert tray.detail == "Downloading 4 of 20"
    assert tray.native_icon.title == "Test Skin Manager - Downloading 4 of 20"
    assert tray.native_icon.icon == "image:SYNCING"
    assert menu_items(tray)[1].text == "Status: Downloading 4 of 20"
    assert fake_icon(tray).menu_updates == 1


def test_sync_now_never_invokes_the_explicit_ltk_port_action() -> None:
    backend = FakeBackend()
    calls: list[str] = []
    tray = make_tray(
        backend,
        on_sync=lambda: calls.append("sync"),
        on_migrate_to_ltk=lambda: calls.append("port"),
    )

    click(tray, menu_items(tray)[2])

    assert calls == ["sync"]


def test_menu_actions_toggle_startup_and_exit_only_once() -> None:
    backend = FakeBackend()
    calls: list[str] = []
    startup = {"enabled": False}

    def set_startup(enabled: bool) -> bool:
        startup["enabled"] = enabled
        calls.append(f"startup:{enabled}")
        return True

    tray = make_tray(
        backend,
        on_show=lambda: calls.append("show"),
        on_sync=lambda: calls.append("sync"),
        on_start_manager=lambda: calls.append("manager"),
        on_open_cslol_skins=lambda: calls.append("cslol-folder"),
        on_start_ltk=lambda: calls.append("ltk"),
        on_open_ltk_install=lambda: calls.append("ltk-app-folder"),
        on_open_ltk_storage=lambda: calls.append("ltk-storage"),
        on_migrate_to_ltk=lambda: calls.append("migrate"),
        on_remove_ltk_skins=lambda: calls.append("clean-ltk"),
        startup_enabled=lambda: startup["enabled"],
        set_startup_enabled=set_startup,
        on_exit=lambda: calls.append("exit"),
    )
    items = menu_items(tray)
    cslol_items = submenu(items[3])
    ltk_items = submenu(items[4])

    click(tray, items[0])
    click(tray, items[2])
    click(tray, cslol_items[0])
    click(tray, cslol_items[1])
    click(tray, ltk_items[0])
    click(tray, ltk_items[1])
    click(tray, ltk_items[2])
    click(tray, ltk_items[3])
    click(tray, ltk_items[4])
    click(tray, items[5])
    refreshed_startup = menu_items(tray)[5]
    checked = refreshed_startup.options["checked"]
    assert callable(checked)
    assert checked(refreshed_startup) is True
    click(tray, items[7])
    click(tray, items[7])

    assert calls == [
        "show",
        "sync",
        "manager",
        "cslol-folder",
        "ltk",
        "ltk-app-folder",
        "ltk-storage",
        "migrate",
        "clean-ltk",
        "startup:True",
        "exit",
    ]
    assert fake_icon(tray).stop_calls == 1


def test_callback_errors_are_not_raised_from_tray_handlers() -> None:
    backend = FakeBackend()

    def fail() -> None:
        raise RuntimeError("sync exploded")

    tray = make_tray(backend, on_sync=fail)

    click(tray, menu_items(tray)[2])

    assert fake_icon(tray).notifications == [
        (
            "LeagueSkinManagerVN",
            "Could not complete skin sync: sync exploded",
        )
    ]


def test_failed_startup_toggle_keeps_existing_state_and_notifies() -> None:
    backend = FakeBackend()
    tray = make_tray(
        backend,
        startup_enabled=lambda: False,
        set_startup_enabled=lambda _enabled: False,
    )

    click(tray, menu_items(tray)[5])

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
    exit_item = menu_items(tray)[7]

    click(tray, exit_item)
    assert exit_calls == 1
    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications[-1] == (
        "Action not completed",
        "Background work is still stopping. The app remains active; try Exit again.",
    )

    click(tray, exit_item)
    assert exit_calls == 2
    assert fake_icon(tray).stop_calls == 1


def test_shutdown_callback_error_does_not_stop_tray() -> None:
    backend = FakeBackend()

    def fail_exit() -> bool:
        raise RuntimeError("worker state unavailable")

    tray = make_tray(backend, on_exit=fail_exit)

    click(tray, menu_items(tray)[7])

    assert fake_icon(tray).stop_calls == 0
    assert fake_icon(tray).notifications[-1] == (
        "LeagueSkinManagerVN",
        "Could not complete application shutdown: worker state unavailable",
    )


def test_tray_uninstall_uses_the_same_bounded_shutdown_path() -> None:
    backend = FakeBackend()
    calls: list[str] = []

    def uninstall() -> bool:
        calls.append("uninstall")
        return True

    tray = make_tray(backend, on_uninstall=uninstall)

    uninstall_item = menu_items(tray)[6]
    click(tray, uninstall_item)
    click(tray, uninstall_item)

    assert calls == ["uninstall"]
    assert fake_icon(tray).stop_calls == 1
