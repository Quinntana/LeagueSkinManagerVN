"""System tray presentation - the application's only user interface.

The tray owns the application lifetime.  Every row here is either a one-line
status summary or a direct action; nothing in this module knows how any of the
work is performed.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from threading import RLock
from typing import Any, Protocol, cast

from .config import APP_NAME
from .controller import AppState


class TrayIcon(Protocol):
    icon: object
    title: str
    menu: object
    visible: bool

    def run(self, setup: Callable[[TrayIcon], None] | None = None) -> None: ...

    def run_detached(self, setup: Callable[[TrayIcon], None] | None = None) -> None: ...

    def stop(self) -> None: ...

    def notify(self, message: str, title: str | None = None) -> None: ...

    def update_menu(self) -> None: ...


class TrayBackend(Protocol):
    Icon: Callable[[str, object, str, object], TrayIcon]
    # Callable that also carries a ``SEPARATOR`` sentinel attribute.
    Menu: Any
    MenuItem: Callable[..., object]


ImageFactory = Callable[[AppState], object]
Action = Callable[[], object]
StartupGetter = Callable[[], bool]
StartupSetter = Callable[[bool], object]

MENU_TEXT_LIMIT = 96
TITLE_LIMIT = 127

_STATE_COLORS: dict[AppState, tuple[int, int, int]] = {
    AppState.STARTING: (30, 144, 255),
    AppState.OFFLINE_READY: (100, 149, 237),
    AppState.SYNCING: (255, 193, 7),
    AppState.READY: (46, 204, 113),
    AppState.ERROR: (231, 76, 60),
    AppState.STOPPING: (127, 140, 141),
}

_STATE_WORDS: dict[AppState, str] = {
    AppState.STARTING: "Starting",
    AppState.OFFLINE_READY: "Offline",
    AppState.SYNCING: "Syncing",
    AppState.READY: "Ready",
    AppState.ERROR: "Error",
    AppState.STOPPING: "Stopping",
}


def make_status_icon(state: AppState, size: int = 64) -> object:
    """Create a compact colored icon without requiring a bundled image asset."""

    if size < 16:
        raise ValueError("size must be at least 16")
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = max(3, size // 8)
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=_STATE_COLORS[state],
        outline=(255, 255, 255, 230),
        width=max(1, size // 24),
    )
    return image


class TrayApplication:
    """Render status and translate menu clicks into injected callbacks.

    ``pystray.Icon.run`` makes the icon visible before invoking its setup
    callback, so slow initialization begins through ``on_start`` only once the
    tray is already available to the user.
    """

    def __init__(
        self,
        *,
        on_start: Action,
        on_sync: Action,
        on_open_ltk: Action,
        on_open_cooldowns: Action,
        on_open_ltk_skins: Action,
        on_open_cslol_skins: Action,
        on_open_data: Action,
        on_open_log: Action,
        on_rebuild_library: Action,
        on_remove_ltk_skins: Action,
        on_uninstall: Action,
        on_exit: Action,
        startup_enabled: StartupGetter,
        set_startup_enabled: StartupSetter,
        runtime_label: str,
        uninstall_available: bool,
        startup_available: bool,
        backend: TrayBackend | None = None,
        image_factory: ImageFactory = make_status_icon,
        app_name: str = APP_NAME,
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_sync = on_sync
        self._on_open_ltk = on_open_ltk
        self._on_open_cooldowns = on_open_cooldowns
        self._on_open_ltk_skins = on_open_ltk_skins
        self._on_open_cslol_skins = on_open_cslol_skins
        self._on_open_data = on_open_data
        self._on_open_log = on_open_log
        self._on_rebuild_library = on_rebuild_library
        self._on_remove_ltk_skins = on_remove_ltk_skins
        self._on_uninstall = on_uninstall
        self._on_exit = on_exit
        self._startup_enabled = startup_enabled
        self._set_startup_enabled = set_startup_enabled
        if not runtime_label.strip():
            raise ValueError("runtime_label must not be empty")
        self._runtime_label = runtime_label.strip()
        self._uninstall_available = bool(uninstall_available)
        self._startup_available = bool(startup_available)
        self._backend = backend or self._load_backend()
        self._image_factory = image_factory
        self._app_name = app_name
        self._logger = logger or logging.getLogger(__name__)

        self._lock = RLock()
        self._state = AppState.STARTING
        self._detail = "Starting"
        self._skin_count: int | None = None
        self._catalog_patch: str | None = None
        self._ltk_installed = True
        self._ltk_activity: str | None = "checking the latest official release"
        self._ltk_busy = False
        self._ltk_in_library: int | None = None
        self._ltk_enabled: int | None = None
        self._ltk_expected: int | None = None
        self._ltk_pending: int | None = None
        self._exit_requested = False
        self._stopped = False
        self._icon = self._backend.Icon(
            self._app_name,
            self._image_factory(self._state),
            self._title(),
            self._build_menu(),
        )

    @property
    def native_icon(self) -> TrayIcon:
        return self._icon

    @property
    def state(self) -> AppState:
        with self._lock:
            return self._state

    @property
    def detail(self) -> str:
        with self._lock:
            return self._detail

    def run(self) -> None:
        """Run the platform tray loop on the calling thread."""

        self._icon.run(setup=self._setup)

    def run_detached(self) -> None:
        """Start the platform tray loop beside another GUI main loop."""

        self._icon.run_detached(setup=self._setup)

    def stop(self) -> None:
        """Stop the platform tray loop; safe to call more than once."""

        with self._lock:
            if self._stopped:
                return
            self._stopped = True
        try:
            self._icon.stop()
        except Exception:
            self._logger.exception("Unable to stop tray icon")

    # ----------------------------------------------------------------- status

    def update_status(self, state: AppState, detail: str) -> None:
        """Status sink suitable for :class:`AppController`."""

        with self._lock:
            self._state = state
            self._detail = detail
            title = self._title()
            image = self._image_factory(state)
        try:
            self._icon.title = title
            self._icon.icon = image
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray status")

    def update_library(self, skin_count: int, patch: str | None) -> None:
        """Publish the local catalog summary without doing menu-time I/O."""

        if skin_count < 0:
            raise ValueError("skin_count cannot be negative")
        with self._lock:
            self._skin_count = skin_count
            self._catalog_patch = patch.strip() if patch and patch.strip() else None
            title = self._title()
        try:
            self._icon.title = title
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray catalog summary")

    def update_ltk_status(self, detail: str, *, rebuild_active: bool = False) -> None:
        """Publish transient LTK activity, shown in place of the library summary."""

        normalized = detail.strip() or "status unavailable"
        with self._lock:
            self._ltk_activity = normalized
            self._ltk_busy = bool(rebuild_active)
        try:
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray LTK status")

    def update_ltk_library(
        self,
        *,
        installed: bool,
        in_library: int | None = None,
        enabled: int | None = None,
        expected: int | None = None,
        pending: int | None = None,
    ) -> None:
        """Publish the LTK library summary: how many skins, and how many are on."""

        counts = (in_library, enabled, expected, pending)
        for value in counts:
            if value is None:
                continue
            if isinstance(value, bool) or value < 0:
                raise ValueError("LTK library counts must be non-negative integers")
        with self._lock:
            self._ltk_installed = bool(installed)
            self._ltk_in_library = in_library
            self._ltk_enabled = enabled
            self._ltk_expected = expected
            self._ltk_pending = pending
            title = self._title()
        try:
            self._icon.title = title
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray LTK library summary")

    def notify(self, title: str, message: str) -> None:
        """Notification sink suitable for :class:`AppController`."""

        try:
            self._icon.notify(message, title)
        except Exception:
            self._logger.exception("Unable to display tray notification")

    # ------------------------------------------------------------------- menu

    def _setup(self, icon: TrayIcon) -> None:
        icon.visible = True
        self._invoke("application startup", self._on_start)

    def _build_menu(self) -> object:
        with self._lock:
            state = self._state
            sync_line = self._sync_line()
            ltk_line = self._ltk_line()
            ltk_installed = self._ltk_installed
        backend = self._backend
        separator = getattr(backend.Menu, "SEPARATOR", None)

        folders = backend.Menu(
            backend.MenuItem("Skins in LTK", self._open_ltk_skins_clicked),
            backend.MenuItem("Skins in CSLOL", self._open_cslol_skins_clicked),
            backend.MenuItem("App data", self._open_data_clicked),
            backend.MenuItem("Diagnostics log", self._open_log_clicked),
        )
        advanced_items: list[object] = [
            backend.MenuItem("Rebuild LTK library now", self._rebuild_clicked),
        ]
        if separator is not None:
            advanced_items.append(separator)
        advanced_items.extend(
            (
                backend.MenuItem("Remove all skins from LTK...", self._remove_ltk_skins_clicked),
                backend.MenuItem(
                    f"Uninstall {self._app_name}...",
                    self._uninstall_clicked,
                    enabled=self._uninstall_available,
                ),
            )
        )
        advanced = backend.Menu(*advanced_items)

        rows: list[object] = [
            backend.MenuItem(sync_line, None, enabled=False),
            backend.MenuItem(ltk_line, None, enabled=False),
        ]
        if separator is not None:
            rows.append(separator)
        rows.extend(
            (
                backend.MenuItem(
                    "Open LTK Manager" if ltk_installed else "Install LTK Manager...",
                    self._open_ltk_clicked,
                    default=True,
                ),
                backend.MenuItem(
                    "Sync skins now",
                    self._sync_clicked,
                    enabled=state not in (AppState.STARTING, AppState.SYNCING, AppState.STOPPING),
                ),
                backend.MenuItem("Enemy cooldown timers...", self._cooldowns_clicked),
            )
        )
        if separator is not None:
            rows.append(separator)
        rows.extend(
            (
                backend.MenuItem("Folders", folders),
                backend.MenuItem("Advanced", advanced),
            )
        )
        if separator is not None:
            rows.append(separator)
        rows.extend(
            (
                backend.MenuItem(
                    "Start with Windows",
                    self._startup_clicked,
                    checked=self._startup_checked,
                    enabled=self._startup_available,
                ),
                backend.MenuItem("Exit", self._exit_clicked),
            )
        )
        return backend.Menu(*rows)

    def _refresh_menu(self) -> None:
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except (AttributeError, NotImplementedError):
            # Some pystray backends refresh automatically when ``menu`` changes.
            return

    def _sync_line(self) -> str:
        """Render the first status row: sync state, skin count, and patch."""

        if self._state is AppState.ERROR:
            return self._clip(f"Error: {self._detail}", MENU_TEXT_LIMIT)
        word = _STATE_WORDS[self._state]
        if self._skin_count is None:
            return f"{word} - reading local catalog"
        parts = [word, f"{self._skin_count:,} skins"]
        if self._catalog_patch:
            parts.append(f"patch {self._catalog_patch}")
        return self._clip(" - ".join(parts), MENU_TEXT_LIMIT)

    def _ltk_line(self) -> str:
        """Render the second status row: LTK activity, or the library summary."""

        if self._ltk_busy and self._ltk_activity:
            return self._clip(f"LTK: {self._ltk_activity}", MENU_TEXT_LIMIT)
        if not self._ltk_installed:
            return "LTK: not installed"
        if self._ltk_in_library is None:
            if self._ltk_activity:
                return self._clip(f"LTK: {self._ltk_activity}", MENU_TEXT_LIMIT)
            return "LTK: status unavailable"
        if self._ltk_pending:
            expected = self._ltk_expected or self._ltk_in_library
            return self._clip(
                f"LTK: {self._ltk_in_library:,} of {expected:,} skins - "
                f"{self._ltk_pending:,} to rebuild",
                MENU_TEXT_LIMIT,
            )
        enabled = 0 if self._ltk_enabled is None else self._ltk_enabled
        return self._clip(
            f"LTK: {self._ltk_in_library:,} skins - {enabled:,} enabled",
            MENU_TEXT_LIMIT,
        )

    def _title(self) -> str:
        parts = [self._app_name, _STATE_WORDS[self._state]]
        if self._skin_count is not None:
            parts.append(f"{self._skin_count:,} skins")
        if self._ltk_pending:
            parts.append("rebuild pending")
        parts.append(self._runtime_label)
        return self._clip(" | ".join(parts), TITLE_LIMIT)

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    # ---------------------------------------------------------------- handlers

    def _sync_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("skin sync", self._on_sync)

    def _open_ltk_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening LTK Manager", self._on_open_ltk)

    def _cooldowns_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the cooldown timers", self._on_open_cooldowns)

    def _open_ltk_skins_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the LTK skin folder", self._on_open_ltk_skins)

    def _open_cslol_skins_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the CSLOL skin folder", self._on_open_cslol_skins)

    def _open_data_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the application data folder", self._on_open_data)

    def _open_log_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the diagnostics log", self._on_open_log)

    def _rebuild_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("rebuilding the LTK library", self._on_rebuild_library)

    def _remove_ltk_skins_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("removing all skins from LTK", self._on_remove_ltk_skins)

    def _startup_checked(self, _item: object) -> bool:
        try:
            return bool(self._startup_enabled())
        except Exception:
            self._logger.exception("Unable to read Start with Windows setting")
            return False

    def _startup_clicked(self, _icon: TrayIcon, _item: object) -> None:
        try:
            desired = not self._startup_enabled()
            result = self._set_startup_enabled(desired)
            if result is False:
                self.notify("Start with Windows", "The startup setting could not be updated.")
                return
            self._refresh_menu()
        except Exception as exc:
            self._logger.exception("Unable to update Start with Windows setting")
            self.notify("Start with Windows", f"Could not update setting: {exc}")

    def _exit_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._request_shutdown(
            self._on_exit,
            failure_message=(
                "Background work is still stopping. The app remains active; try Exit again."
            ),
        )

    def _uninstall_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._request_shutdown(
            self._on_uninstall,
            failure_message=f"The uninstaller was not started. {self._app_name} remains active.",
        )

    def _request_shutdown(self, callback: Action, *, failure_message: str) -> None:
        with self._lock:
            if self._exit_requested:
                return
            self._exit_requested = True
        try:
            try:
                result = callback()
            except Exception as exc:
                self._logger.exception("Tray callback failed during application shutdown")
                self.notify(self._app_name, f"Could not complete application shutdown: {exc}")
                return
            if result is False:
                self.notify("Action not completed", failure_message)
                return
            self.stop()
        finally:
            with self._lock:
                if not self._stopped:
                    self._exit_requested = False

    def _invoke(self, description: str, callback: Action) -> object | None:
        try:
            return callback()
        except Exception as exc:
            self._logger.exception("Tray callback failed during %s", description)
            self.notify(self._app_name, f"Could not complete {description}: {exc}")
            return None

    @staticmethod
    def _load_backend() -> TrayBackend:
        return cast(TrayBackend, importlib.import_module("pystray"))


__all__ = [
    "MENU_TEXT_LIMIT",
    "TITLE_LIMIT",
    "Action",
    "ImageFactory",
    "StartupGetter",
    "StartupSetter",
    "TrayApplication",
    "TrayBackend",
    "TrayIcon",
    "make_status_icon",
]
