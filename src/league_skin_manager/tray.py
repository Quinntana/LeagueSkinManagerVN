"""Small, callback-driven system tray presentation layer."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from threading import RLock
from typing import Protocol, cast

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
    Menu: Callable[..., object]
    MenuItem: Callable[..., object]


ImageFactory = Callable[[AppState], object]
Action = Callable[[], object]
StartupGetter = Callable[[], bool]
StartupSetter = Callable[[bool], object]


_STATE_COLORS: dict[AppState, tuple[int, int, int]] = {
    AppState.STARTING: (30, 144, 255),
    AppState.OFFLINE_READY: (100, 149, 237),
    AppState.SYNCING: (255, 193, 7),
    AppState.READY: (46, 204, 113),
    AppState.ERROR: (231, 76, 60),
    AppState.STOPPING: (127, 140, 141),
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
    """Render lifecycle status and translate menu clicks into safe callbacks.

    ``pystray.Icon.run`` makes the icon visible before invoking its setup
    callback.  Slow initialization therefore begins through ``on_start`` only
    after the tray is available to the user.
    """

    def __init__(
        self,
        *,
        on_start: Action,
        on_show: Action,
        on_sync: Action,
        on_start_manager: Action,
        on_open_cslol_skins: Action,
        on_copy_cslol_manager_path: Action,
        on_start_ltk: Action,
        on_open_ltk_install: Action,
        on_open_ltk_storage: Action,
        on_migrate_to_ltk: Action,
        on_cancel_ltk_migration: Action,
        on_reset_ltk_migration: Action,
        on_remove_ltk_skins: Action,
        on_open_data: Action,
        on_open_log: Action,
        startup_enabled: StartupGetter,
        set_startup_enabled: StartupSetter,
        on_uninstall: Action,
        on_exit: Action,
        runtime_label: str,
        uninstall_available: bool,
        startup_available: bool,
        backend: TrayBackend | None = None,
        image_factory: ImageFactory = make_status_icon,
        app_name: str = APP_NAME,
        logger: logging.Logger | None = None,
    ) -> None:
        self._on_start = on_start
        self._on_show = on_show
        self._on_sync = on_sync
        self._on_start_manager = on_start_manager
        self._on_open_cslol_skins = on_open_cslol_skins
        self._on_copy_cslol_manager_path = on_copy_cslol_manager_path
        self._on_start_ltk = on_start_ltk
        self._on_open_ltk_install = on_open_ltk_install
        self._on_open_ltk_storage = on_open_ltk_storage
        self._on_migrate_to_ltk = on_migrate_to_ltk
        self._on_cancel_ltk_migration = on_cancel_ltk_migration
        self._on_reset_ltk_migration = on_reset_ltk_migration
        self._on_remove_ltk_skins = on_remove_ltk_skins
        self._on_open_data = on_open_data
        self._on_open_log = on_open_log
        self._startup_enabled = startup_enabled
        self._set_startup_enabled = set_startup_enabled
        self._on_uninstall = on_uninstall
        self._on_exit = on_exit
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
        self._ltk_detail = "checking the latest official release"
        self._ltk_migration_active = False
        self._ltk_port_pending: int | None = None
        self._ltk_port_total: int | None = None
        self._ltk_port_unavailable = False
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
        """Publish a cached local catalog summary without doing menu-time I/O."""

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

    def update_ltk_status(self, detail: str, *, migration_active: bool = False) -> None:
        """Publish LTK companion state independently from VN skin sync state."""

        normalized = detail.strip() or "status unavailable"
        with self._lock:
            self._ltk_detail = normalized
            self._ltk_migration_active = bool(migration_active)
        try:
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray LTK status")

    def update_ltk_port_status(
        self,
        *,
        pending: int | None,
        total: int | None,
        unavailable: bool = False,
    ) -> None:
        """Publish whether current VN-managed skins still need an explicit LTK port."""

        if unavailable:
            if pending is not None or total is not None:
                raise ValueError("unavailable LTK port status cannot include counts")
        elif pending is None or total is None:
            raise ValueError("LTK port status requires both pending and total counts")
        elif (
            isinstance(pending, bool)
            or isinstance(total, bool)
            or pending < 0
            or total < 0
            or pending > total
        ):
            raise ValueError("LTK port counts must satisfy 0 <= pending <= total")

        with self._lock:
            self._ltk_port_pending = pending
            self._ltk_port_total = total
            self._ltk_port_unavailable = bool(unavailable)
            title = self._title()
        try:
            self._icon.title = title
            self._refresh_menu()
        except Exception:
            self._logger.exception("Unable to refresh tray LTK port status")

    def notify(self, title: str, message: str) -> None:
        """Notification sink suitable for :class:`AppController`."""

        try:
            self._icon.notify(message, title)
        except Exception:
            self._logger.exception("Unable to display tray notification")

    def _setup(self, icon: TrayIcon) -> None:
        icon.visible = True
        self._invoke("application startup", self._on_start)

    def _build_menu(self) -> object:
        with self._lock:
            detail = self._detail
            state = self._state
            skin_count = self._skin_count
            catalog_patch = self._catalog_patch
            ltk_detail = self._ltk_detail
            migration_active = self._ltk_migration_active
            ltk_port_pending = self._ltk_port_pending
            ltk_port_total = self._ltk_port_total
            ltk_port_unavailable = self._ltk_port_unavailable
        cslol_menu = self._backend.Menu(
            self._backend.MenuItem("Open CSLOL Manager", self._start_manager_clicked),
            self._backend.MenuItem(
                "Open installed skins folder",
                self._open_cslol_skins_clicked,
            ),
            self._backend.MenuItem(
                "Copy CSLOL Manager folder path",
                self._copy_cslol_manager_path_clicked,
            ),
        )
        ltk_menu = self._backend.Menu(
            self._backend.MenuItem("Open or install LTK Manager", self._start_ltk_clicked),
            self._backend.MenuItem(
                "Open LTK application folder",
                self._open_ltk_install_clicked,
            ),
            self._backend.MenuItem(
                "Open LTK skin storage folder",
                self._open_ltk_storage_clicked,
            ),
            self._backend.MenuItem(
                self._ltk_port_action_label(ltk_port_pending),
                self._migrate_to_ltk_clicked,
            ),
            self._backend.MenuItem(
                "Cancel active LTK port",
                self._cancel_ltk_migration_clicked,
                enabled=migration_active,
            ),
        )
        maintenance_menu = self._backend.Menu(
            self._backend.MenuItem(
                "Open LeagueSkinManagerVN data folder",
                self._open_data_clicked,
            ),
            self._backend.MenuItem(
                "Open diagnostics log",
                self._open_log_clicked,
            ),
            self._backend.MenuItem(
                "Reset LTK port history...",
                self._reset_ltk_migration_clicked,
            ),
            self._backend.MenuItem(
                "Remove all LTK skins...",
                self._remove_ltk_skins_clicked,
            ),
            self._backend.MenuItem(
                "Uninstall LeagueSkinManagerVN...",
                self._uninstall_clicked,
                enabled=self._uninstall_available,
            ),
        )
        return self._backend.Menu(
            self._backend.MenuItem(
                "Browse/search VN skin library...",
                self._show_clicked,
                default=True,
            ),
            self._backend.MenuItem(
                self._clip_menu_text(f"Sync: {detail}"),
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                self._library_label(skin_count, catalog_patch),
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                self._clip_menu_text(f"LTK: {ltk_detail}"),
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                self._ltk_port_label(
                    ltk_port_pending,
                    ltk_port_total,
                    unavailable=ltk_port_unavailable,
                ),
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                f"Runtime: {self._runtime_label}",
                None,
                enabled=False,
            ),
            self._backend.MenuItem(
                "Sync VN skins now",
                self._sync_clicked,
                enabled=state not in (AppState.STARTING, AppState.SYNCING, AppState.STOPPING),
            ),
            self._backend.MenuItem("CSLOL Manager", cslol_menu),
            self._backend.MenuItem("LTK Manager", ltk_menu),
            self._backend.MenuItem("Maintenance", maintenance_menu),
            self._backend.MenuItem(
                "Start with Windows",
                self._startup_clicked,
                checked=self._startup_checked,
                enabled=self._startup_available,
            ),
            self._backend.MenuItem("Exit", self._exit_clicked),
        )

    def _refresh_menu(self) -> None:
        self._icon.menu = self._build_menu()
        try:
            self._icon.update_menu()
        except (AttributeError, NotImplementedError):
            # Some pystray backends refresh automatically when ``menu`` changes.
            return

    def _title(self) -> str:
        parts = [self._app_name, self._detail]
        if self._skin_count is not None:
            parts.append(f"{self._skin_count:,} skins")
        if self._ltk_port_pending:
            parts.append("LTK port recommended")
        return self._clip_text(" | ".join(parts), 127)

    @classmethod
    def _clip_menu_text(cls, text: str) -> str:
        return cls._clip_text(text, 96)

    @staticmethod
    def _clip_text(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @classmethod
    def _library_label(cls, skin_count: int | None, patch: str | None) -> str:
        if skin_count is None:
            return "Library: reading local catalog"
        if skin_count == 0 and patch is None:
            return "Library: not synced yet"
        patch_label = patch or "unknown"
        return cls._clip_menu_text(f"Library: {skin_count:,} VN skins | patch {patch_label}")

    @classmethod
    def _ltk_port_label(
        cls,
        pending: int | None,
        total: int | None,
        *,
        unavailable: bool,
    ) -> str:
        if unavailable:
            return "LTK port: status unavailable; check diagnostics"
        if pending is None or total is None:
            return "LTK port: checking VN handoff state"
        if total == 0:
            return "LTK port: no VN-managed skins to port"
        if pending == 0:
            return cls._clip_menu_text(f"LTK port: all {total:,} current VN skins were queued")
        return cls._clip_menu_text(f"LTK port: {pending:,} of {total:,} VN skins need manual port")

    @staticmethod
    def _ltk_port_action_label(pending: int | None) -> str:
        if pending:
            return f"Port CSLOL skins to LTK now ({pending:,} pending)..."
        return "Port CSLOL skins to LTK now..."

    def _sync_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("skin sync", self._on_sync)

    def _show_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the desktop window", self._on_show)

    def _start_manager_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("CSLOL Manager launch", self._on_start_manager)

    def _open_cslol_skins_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the CSLOL installed skins folder", self._on_open_cslol_skins)

    def _copy_cslol_manager_path_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke(
            "copying the CSLOL Manager folder path",
            self._on_copy_cslol_manager_path,
        )

    def _start_ltk_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("LTK Manager launch", self._on_start_ltk)

    def _open_ltk_install_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the LTK application folder", self._on_open_ltk_install)

    def _open_ltk_storage_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the LTK skin storage folder", self._on_open_ltk_storage)

    def _migrate_to_ltk_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the explicit CSLOL-to-LTK port tool", self._on_migrate_to_ltk)

    def _cancel_ltk_migration_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("cancelling the active LTK port", self._on_cancel_ltk_migration)

    def _reset_ltk_migration_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("resetting LTK port history", self._on_reset_ltk_migration)

    def _remove_ltk_skins_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("removing all LTK skins", self._on_remove_ltk_skins)

    def _open_data_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the application data folder", self._on_open_data)

    def _open_log_clicked(self, _icon: TrayIcon, _item: object) -> None:
        self._invoke("opening the diagnostics log", self._on_open_log)

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
                self.notify(
                    "Start with Windows",
                    "The startup setting could not be updated.",
                )
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
            failure_message="The uninstaller was not started. LeagueSkinManagerVN remains active.",
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
                self.notify(
                    "LeagueSkinManagerVN",
                    f"Could not complete application shutdown: {exc}",
                )
                return
            if result is False:
                self.notify(
                    "Action not completed",
                    failure_message,
                )
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
            self.notify(
                "LeagueSkinManagerVN",
                f"Could not complete {description}: {exc}",
            )
            return None

    @staticmethod
    def _load_backend() -> TrayBackend:
        return cast(TrayBackend, importlib.import_module("pystray"))


__all__ = [
    "Action",
    "ImageFactory",
    "StartupGetter",
    "StartupSetter",
    "TrayApplication",
    "TrayBackend",
    "TrayIcon",
    "make_status_icon",
]
