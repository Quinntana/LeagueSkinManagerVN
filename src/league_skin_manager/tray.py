"""The system tray icon: the application's only permanent interface.

Deliberately not a state machine.  The previous design put a computed status
line at the top of the menu, which raised the question of what to show when two
conditions were true at once and grew priority rules to answer it.

Here the icon colour is a single variable, so two states cannot collide;
detail lives in the tooltip; and failures are notifications rather than
persistent modes.  The menu holds only actions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .settings import OPACITY_CHOICES, SCALE_CHOICES

LOGGER = logging.getLogger(__name__)

TOOLTIP_LIMIT = 127

IDLE_COLOR = (91, 100, 114)
"""Slate. Distinct from amber in lightness as well as hue, so the two states
remain distinguishable in greyscale and for common colour-vision deficiencies."""

WORKING_COLOR = (200, 137, 43)


class TrayIcon(Protocol):
    icon: object
    title: str
    menu: object
    visible: bool

    def run(self, setup: Callable[[TrayIcon], None] | None = None) -> None: ...

    def stop(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...

    def update_menu(self) -> None: ...


class TrayBackend(Protocol):
    Icon: Callable[..., TrayIcon]
    Menu: Any
    MenuItem: Callable[..., Any]


def make_icon(working: bool, size: int = 64) -> object:
    """Draw the tray glyph without needing a bundled image asset."""

    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(3, size // 8)
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=WORKING_COLOR if working else IDLE_COLOR,
        outline=(255, 255, 255, 230),
        width=max(1, size // 24),
    )
    return image


@dataclass
class TrayState:
    """Everything the menu needs to render itself."""

    skins: int = 0
    patch: str | None = None
    synced_at: str | None = None
    working: bool = False
    detail: str | None = None
    ltk_installed: bool = False
    blocked_reason: str | None = None
    match_active: bool = False
    cooldown_auto_run: bool = False
    startup_enabled: bool = False
    opacity: float = 0.85
    scale: float = 1.0

    def tooltip(self) -> str:
        if self.blocked_reason:
            text = self.blocked_reason
        elif self.detail:
            text = self.detail
        elif self.skins:
            parts = [f"{self.skins:,} skins"]
            if self.patch:
                parts.append(f"patch {self.patch}")
            if self.synced_at:
                parts.append(f"synced {self.synced_at[:10]}")
            text = " · ".join(parts)
        else:
            text = "No skins installed yet"
        return text[:TOOLTIP_LIMIT]


@dataclass(frozen=True)
class TrayActions:
    """Callbacks the shell supplies. Every one returns quickly."""

    open_ltk: Callable[[], object]
    sync: Callable[[], object]
    open_cooldowns: Callable[[], object]
    get_porofessor: Callable[[], object]
    open_folder: Callable[[], object]
    set_cooldown_auto_run: Callable[[bool], object]
    set_startup: Callable[[bool], object]
    set_opacity: Callable[[float], object]
    set_scale: Callable[[float], object]
    uninstall: Callable[[], object]
    exit_app: Callable[[], object]


@dataclass
class Tray:
    """Builds and drives the tray icon."""

    actions: TrayActions
    state: TrayState = field(default_factory=TrayState)
    backend: Any = None
    logger: logging.Logger = LOGGER
    _icon: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.backend is None:
            import pystray

            self.backend = pystray

    # -- menu ------------------------------------------------------------

    def build_menu(self) -> Any:
        backend = self.backend
        item = backend.MenuItem
        separator = getattr(backend.Menu, "SEPARATOR", None)

        display = backend.Menu(
            item("Opacity", None, enabled=False),
            *[
                item(
                    f"{int(value * 100)}%",
                    self._opacity_setter(value),
                    checked=self._opacity_checker(value),
                    radio=True,
                )
                for value in OPACITY_CHOICES
            ],
            separator,
            item("Size", None, enabled=False),
            *[
                item(
                    f"{int(value * 100)}%",
                    self._scale_setter(value),
                    checked=self._scale_checker(value),
                    radio=True,
                )
                for value in SCALE_CHOICES
            ],
        )

        rows: list[Any] = [
            item(
                "Open LTK Manager" if self.state.ltk_installed else "Install LTK Manager",
                self._wrap(self.actions.open_ltk),
                default=True,
                enabled=not self.state.working,
            ),
            item(
                "Sync skins now",
                self._wrap(self.actions.sync),
                enabled=not self.state.working and self.state.blocked_reason is None,
            ),
            separator,
            item(
                "Cooldown timers",
                self._wrap(self.actions.open_cooldowns),
                enabled=self.state.match_active,
            ),
            item("Get Porofessor", self._wrap(self.actions.get_porofessor)),
            separator,
            item("Open app folder", self._wrap(self.actions.open_folder)),
            separator,
            item(
                "Cooldown timers with game",
                self._toggle_cooldown,
                checked=lambda _item: self.state.cooldown_auto_run,
            ),
            item("Cooldown display", display),
            item(
                "Start with Windows",
                self._toggle_startup,
                checked=lambda _item: self.state.startup_enabled,
            ),
            separator,
            item(
                "Uninstall...", self._wrap(self.actions.uninstall), enabled=not self.state.working
            ),
            item("Exit", self._wrap(self.actions.exit_app)),
        ]
        return backend.Menu(*[row for row in rows if row is not None])

    # -- lifecycle -------------------------------------------------------

    def run(self) -> None:
        self._icon = self.backend.Icon(
            "LeagueSkinManagerVN",
            make_icon(self.state.working),
            self.state.tooltip(),
            self.build_menu(),
        )
        self._icon.run()

    def stop(self) -> None:
        if self._icon is not None:
            with _never_raises(self.logger, "stopping the tray icon"):
                self._icon.stop()

    def refresh(self, **changes: Any) -> None:
        """Apply state changes and redraw the icon, tooltip, and menu."""

        for key, value in changes.items():
            if not hasattr(self.state, key):
                raise AttributeError(f"Unknown tray state: {key}")
            setattr(self.state, key, value)
        icon = self._icon
        if icon is None:
            return
        with _never_raises(self.logger, "refreshing the tray"):
            icon.icon = make_icon(self.state.working)
            icon.title = self.state.tooltip()
            icon.menu = self.build_menu()
            icon.update_menu()

    def notify(self, title: str, message: str) -> None:
        icon = self._icon
        if icon is None:
            return
        with _never_raises(self.logger, "showing a notification"):
            icon.notify(message, title)

    # -- handlers --------------------------------------------------------

    def _wrap(self, action: Callable[[], object]) -> Callable[..., None]:
        def handler(*_args: object) -> None:
            with _never_raises(self.logger, "handling a tray action"):
                action()

        return handler

    def _toggle_cooldown(self, *_args: object) -> None:
        with _never_raises(self.logger, "toggling cooldown auto-run"):
            self.actions.set_cooldown_auto_run(not self.state.cooldown_auto_run)

    def _toggle_startup(self, *_args: object) -> None:
        with _never_raises(self.logger, "toggling Windows startup"):
            self.actions.set_startup(not self.state.startup_enabled)

    def _opacity_setter(self, value: float) -> Callable[..., None]:
        return self._wrap(lambda: self.actions.set_opacity(value))

    def _scale_setter(self, value: float) -> Callable[..., None]:
        return self._wrap(lambda: self.actions.set_scale(value))

    def _opacity_checker(self, value: float) -> Callable[[Any], bool]:
        return lambda _item: abs(self.state.opacity - value) < 1e-6

    def _scale_checker(self, value: float) -> Callable[[Any], bool]:
        return lambda _item: abs(self.state.scale - value) < 1e-6


class _never_raises:  # noqa: N801 - used as a context manager
    """Swallow and log anything a UI callback throws.

    A tray callback that raises can take the icon's event loop with it, which
    would leave the application running with no interface at all.
    """

    def __init__(self, logger: logging.Logger, what: str) -> None:
        self._logger = logger
        self._what = what

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> bool:
        if exc_type is not None:
            self._logger.exception("Failure while %s", self._what)
        return exc_type is not None


__all__ = [
    "IDLE_COLOR",
    "WORKING_COLOR",
    "Tray",
    "TrayActions",
    "TrayBackend",
    "TrayIcon",
    "TrayState",
    "make_icon",
]
