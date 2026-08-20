"""Tests for the tray menu and its state rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from league_skin_manager.settings import OPACITY_CHOICES, SCALE_CHOICES
from league_skin_manager.tray import IDLE_COLOR, WORKING_COLOR, Tray, TrayActions, TrayState

# --- a pystray-shaped stub -------------------------------------------------


@dataclass
class FakeItem:
    text: str
    action: Any = None
    enabled: bool = True
    checked: Any = None
    radio: bool = False
    default: bool = False
    submenu: Any = None


class FakeMenu:
    SEPARATOR = object()

    def __init__(self, *items: Any) -> None:
        self.items = [item for item in items if item is not FakeMenu.SEPARATOR]
        self.raw = list(items)


def fake_item(text: str, action: Any = None, **kwargs: Any) -> Any:
    if isinstance(action, FakeMenu):
        return FakeItem(text=text, submenu=action, **kwargs)
    return FakeItem(text=text, action=action, **kwargs)


@dataclass
class FakeIcon:
    name: str = ""
    icon: Any = None
    title: str = ""
    menu: Any = None
    visible: bool = False
    notifications: list[tuple[str, str]] = field(default_factory=list)
    stopped: bool = False
    updates: int = 0

    def run(self) -> None:
        self.visible = True

    def stop(self) -> None:
        self.stopped = True

    def notify(self, message: str, title: str | None = None) -> None:
        self.notifications.append((title or "", message))

    def update_menu(self) -> None:
        self.updates += 1


class FakeBackend:
    Menu = FakeMenu
    MenuItem = staticmethod(fake_item)

    def __init__(self) -> None:
        self.icon: FakeIcon | None = None

    def Icon(self, name: str, icon: Any, title: str, menu: Any) -> FakeIcon:  # noqa: N802
        self.icon = FakeIcon(name=name, icon=icon, title=title, menu=menu)
        return self.icon


def make_tray(**state: Any) -> tuple[Tray, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {}

    def record(name: str) -> Any:
        calls[name] = []
        return lambda *args: calls[name].append(args)

    actions = TrayActions(
        open_ltk=record("open_ltk"),
        sync=record("sync"),
        open_cooldowns=record("open_cooldowns"),
        get_porofessor=record("get_porofessor"),
        open_folder=record("open_folder"),
        set_cooldown_auto_run=record("set_cooldown_auto_run"),
        set_startup=record("set_startup"),
        set_opacity=record("set_opacity"),
        set_scale=record("set_scale"),
        uninstall=record("uninstall"),
        exit_app=record("exit_app"),
    )
    return Tray(actions=actions, state=TrayState(**state), backend=FakeBackend()), calls


def labels(tray: Tray) -> list[str]:
    return [item.text for item in tray.build_menu().items]


def find(tray: Tray, text: str) -> FakeItem:
    for item in tray.build_menu().items:
        if item.text == text:
            return item
    raise AssertionError(f"no menu item named {text!r}")


# --- menu contents ---------------------------------------------------------


def test_the_menu_holds_only_actions() -> None:
    """No status rows: that design forced priority rules when two applied."""

    tray, _ = make_tray(skins=1922, patch="16.15.1")
    assert "1,922 skins" not in " ".join(labels(tray))


def test_the_expected_actions_are_present() -> None:
    tray, _ = make_tray()
    present = labels(tray)
    for expected in (
        "Sync skins now",
        "Cooldown timers",
        "Get Porofessor",
        "Open app folder",
        "Cooldown display",
        "Start with Windows",
        "Exit",
    ):
        assert expected in present


def test_the_first_action_is_the_left_click_default() -> None:
    tray, _ = make_tray(ltk_installed=True)
    first = tray.build_menu().items[0]
    assert first.text == "Open LTK Manager"
    assert first.default is True


def test_the_label_changes_when_ltk_is_absent() -> None:
    tray, _ = make_tray(ltk_installed=False)
    assert labels(tray)[0] == "Install LTK Manager"


# --- enablement ------------------------------------------------------------


def test_sync_is_disabled_while_working() -> None:
    tray, _ = make_tray(working=True)
    assert find(tray, "Sync skins now").enabled is False


def test_sync_is_disabled_when_blocked() -> None:
    tray, _ = make_tray(blocked_reason="LTK is not ours")
    assert find(tray, "Sync skins now").enabled is False


def test_cooldowns_are_disabled_outside_a_match() -> None:
    tray, _ = make_tray(match_active=False)
    assert find(tray, "Cooldown timers").enabled is False


def test_cooldowns_are_enabled_during_a_match() -> None:
    tray, _ = make_tray(match_active=True)
    assert find(tray, "Cooldown timers").enabled is True


def test_cooldowns_are_disabled_while_the_board_is_on_screen() -> None:
    """One board per game: a second could only disagree with the first."""

    tray, _ = make_tray(match_active=True, cooldown_visible=True)
    assert find(tray, "Cooldown timers").enabled is False


def test_hiding_the_board_offers_the_action_again() -> None:
    tray, _ = make_tray(match_active=True, cooldown_visible=False)
    assert find(tray, "Cooldown timers").enabled is True


def test_a_visible_board_outside_a_match_is_still_disabled() -> None:
    """Both rules apply; neither is a substitute for the other."""

    tray, _ = make_tray(match_active=False, cooldown_visible=True)
    assert find(tray, "Cooldown timers").enabled is False


def test_the_cooldown_half_stays_usable_when_skins_are_blocked() -> None:
    """Isolation shows up in the UI: an LTK problem must not disable the board."""

    tray, _ = make_tray(blocked_reason="LTK is not ours", match_active=True)
    assert find(tray, "Sync skins now").enabled is False
    assert find(tray, "Cooldown timers").enabled is True


def test_uninstall_is_disabled_while_working() -> None:
    tray, _ = make_tray(working=True)
    assert find(tray, "Uninstall...").enabled is False


# --- display submenu -------------------------------------------------------


def test_the_display_submenu_offers_every_preset() -> None:
    tray, _ = make_tray()
    submenu = find(tray, "Cooldown display").submenu
    texts = [item.text for item in submenu.items]
    for value in OPACITY_CHOICES:
        assert f"{int(value * 100)}%" in texts
    for value in SCALE_CHOICES:
        assert f"{int(value * 100)}%" in texts


def test_display_presets_are_radio_items() -> None:
    tray, _ = make_tray()
    submenu = find(tray, "Cooldown display").submenu
    selectable = [item for item in submenu.items if item.text.endswith("%")]
    assert selectable and all(item.radio for item in selectable)


def test_exactly_one_opacity_is_checked() -> None:
    tray, _ = make_tray(opacity=0.70)
    submenu = find(tray, "Cooldown display").submenu
    checked = [item for item in submenu.items if item.radio and item.checked(item)]
    # One opacity and one size are selected.
    assert len(checked) == 2


def test_choosing_an_opacity_passes_the_value() -> None:
    tray, calls = make_tray()
    submenu = find(tray, "Cooldown display").submenu
    # 55% is offered for opacity only, so the target is unambiguous.
    next(item for item in submenu.items if item.text == "55%").action()
    assert calls["set_opacity"] == [(0.55,)]
    assert calls["set_scale"] == []


def test_choosing_a_size_passes_the_value() -> None:
    tray, calls = make_tray()
    submenu = find(tray, "Cooldown display").submenu
    # 125% is offered for size only.
    next(item for item in submenu.items if item.text == "125%").action()
    assert calls["set_scale"] == [(1.25,)]
    assert calls["set_opacity"] == []


# --- toggles ---------------------------------------------------------------


def test_toggling_cooldown_auto_run_inverts_it() -> None:
    tray, calls = make_tray(cooldown_auto_run=False)
    find(tray, "Cooldown timers with game").action()
    assert calls["set_cooldown_auto_run"] == [(True,)]


def test_toggling_startup_inverts_it() -> None:
    tray, calls = make_tray(startup_enabled=True)
    find(tray, "Start with Windows").action()
    assert calls["set_startup"] == [(False,)]


def test_checkboxes_reflect_state() -> None:
    tray, _ = make_tray(cooldown_auto_run=True, startup_enabled=False)
    assert find(tray, "Cooldown timers with game").checked(None) is True
    assert find(tray, "Start with Windows").checked(None) is False


# --- tooltip ---------------------------------------------------------------


def test_the_tooltip_carries_the_counts() -> None:
    state = TrayState(skins=1922, patch="16.15.1", synced_at="2026-08-19T08:00:00Z")
    tooltip = state.tooltip()
    assert "1,922 skins" in tooltip
    assert "patch 16.15.1" in tooltip
    assert "2026-08-19" in tooltip


def test_a_blocked_reason_wins_the_tooltip() -> None:
    state = TrayState(skins=1922, blocked_reason="LTK is not ours")
    assert state.tooltip() == "LTK is not ours"


def test_detail_beats_the_counts() -> None:
    state = TrayState(skins=1922, detail="Downloading skins… 400 / 1,935")
    assert "Downloading" in state.tooltip()


def test_a_fresh_install_says_so() -> None:
    assert TrayState().tooltip() == "No skins installed yet"


def test_the_tooltip_is_truncated_to_the_windows_limit() -> None:
    assert len(TrayState(blocked_reason="x" * 500).tooltip()) == 127


# --- icon ------------------------------------------------------------------


def test_the_icon_has_two_states_distinguished_by_lightness() -> None:
    """Hue alone fails for common colour-vision deficiencies."""

    assert IDLE_COLOR != WORKING_COLOR
    assert sum(WORKING_COLOR) > sum(IDLE_COLOR)


# --- lifecycle -------------------------------------------------------------


def test_running_creates_the_icon() -> None:
    tray, _ = make_tray(skins=5)
    tray.run()
    assert tray.backend.icon is not None
    assert tray.backend.icon.visible


def test_refresh_updates_the_icon_and_menu() -> None:
    tray, _ = make_tray()
    tray.run()
    tray.refresh(working=True, skins=1922)
    icon = tray.backend.icon
    assert icon is not None
    assert icon.updates == 1
    assert "1,922 skins" in icon.title or tray.state.working


def test_refresh_rejects_unknown_state() -> None:
    tray, _ = make_tray()
    with pytest.raises(AttributeError, match="Unknown tray state"):
        tray.refresh(nonsense=True)


def test_refresh_before_run_is_harmless() -> None:
    tray, _ = make_tray()
    tray.refresh(skins=10)
    assert tray.state.skins == 10


def test_notify_reaches_the_icon() -> None:
    tray, _ = make_tray()
    tray.run()
    tray.notify("Title", "Message")
    assert tray.backend.icon is not None
    assert tray.backend.icon.notifications == [("Title", "Message")]


def test_stop_stops_the_icon() -> None:
    tray, _ = make_tray()
    tray.run()
    tray.stop()
    assert tray.backend.icon is not None
    assert tray.backend.icon.stopped


def test_a_raising_action_does_not_escape() -> None:
    """A callback that raises would otherwise kill the icon's event loop."""

    tray, _ = make_tray()

    def explode() -> None:
        raise RuntimeError("boom")

    handler = tray._wrap(explode)
    handler()  # must not raise
