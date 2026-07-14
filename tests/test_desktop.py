from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import Any

import pytest

from league_skin_manager.catalog import CatalogError, CatalogSnapshot, SkinRecord
from league_skin_manager.controller import AppState
from league_skin_manager.desktop import DesktopApplication, format_package_size


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024 * 1024, "1.0 MB"),
    ),
)
def test_format_package_size(value: int, expected: str) -> None:
    assert format_package_size(value) == expected


def test_format_package_size_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="negative"):
        format_package_size(-1)


class FakeVar:
    def __init__(self, value: object = "") -> None:
        self.value = value

    def get(self) -> object:
        return self.value

    def set(self, value: object) -> None:
        self.value = value


class FakeBox:
    def __init__(self) -> None:
        self.values: tuple[str, ...] = ()

    def configure(self, **values: object) -> None:
        self.values = values["values"]  # type: ignore[assignment]


class FakeButton:
    def __init__(self) -> None:
        self.state = "disabled"

    def configure(self, **values: object) -> None:
        self.state = str(values["state"])


class FakeTree:
    def __init__(self) -> None:
        self.items: dict[str, tuple[object, ...]] = {}
        self.selected: tuple[str, ...] = ()

    def get_children(self) -> tuple[str, ...]:
        return tuple(self.items)

    def delete(self, *items: str) -> None:
        for item in items:
            self.items.pop(item, None)

    def insert(
        self,
        _parent: str,
        _position: str,
        *,
        iid: str,
        values: tuple[object, ...],
    ) -> None:
        self.items[iid] = values

    def selection(self) -> tuple[str, ...]:
        return self.selected


class FakeRoot:
    def __init__(self) -> None:
        self.actions: list[str] = []
        self.cancelled: list[str] = []

    def after(self, _milliseconds: int, _callback: object) -> str:
        self.actions.append("after")
        return "after-id"

    def after_cancel(self, identifier: str) -> None:
        self.cancelled.append(identifier)

    def deiconify(self) -> None:
        self.actions.append("show")

    def lift(self) -> None:
        self.actions.append("lift")

    def focus_force(self) -> None:
        self.actions.append("focus")

    def withdraw(self) -> None:
        self.actions.append("hide")

    def destroy(self) -> None:
        self.actions.append("destroy")


def record(champion: str, name: str, size: int) -> SkinRecord:
    return SkinRecord(
        champion=champion,
        name=name,
        source_path=f"skins/{champion}/{name}.fantome",
        directory=f"directory-{champion}-{name}",
        size=size,
        content_sha256="a" * 64,
    )


def make_desktop(tmp_path: Path, **overrides: Any) -> DesktopApplication:
    values: dict[str, Any] = {
        "catalog_path": tmp_path / "managed_skins.json",
        "installed_dir": tmp_path / "installed",
        "data_dir": tmp_path / "data",
        "log_file": tmp_path / "data" / "logs" / "LeagueSkinManagerVN.log",
        "on_sync": lambda: True,
        "on_start_manager": lambda: True,
        "on_start_ltk": lambda: True,
        "on_migrate_to_ltk": lambda _path: True,
        "on_cancel_ltk_migration": lambda: True,
        "on_reset_ltk_migration": lambda: True,
        "on_exit": lambda: True,
        "startup_enabled": lambda: False,
        "set_startup_enabled": lambda _enabled: True,
        "path_opener": lambda _path: None,
        "catalog_loader": lambda _path: CatalogSnapshot("", None, ()),
    }
    values.update(overrides)
    return DesktopApplication(**values)


def attach_presenter_fakes(app: DesktopApplication) -> tuple[FakeRoot, FakeTree]:
    root = FakeRoot()
    tree = FakeTree()
    app._root = root
    app._tree = tree
    app._search_var = FakeVar("")
    app._champion_var = FakeVar(app.ALL_CHAMPIONS)
    app._result_var = FakeVar()
    app._status_var = FakeVar()
    app._stats_var = FakeVar()
    app._detail_var = FakeVar()
    app._startup_var = FakeVar(False)
    app._ltk_status_var = FakeVar()
    app._cancel_migration_button = FakeButton()
    app._champion_box = FakeBox()
    return root, tree


def finish_exit_request(app: DesktopApplication) -> None:
    worker = app._exit_thread
    assert worker is not None
    worker.join(1.0)
    assert not worker.is_alive()
    app._drain_events()


def test_presenter_loads_filters_sorts_and_selects_catalog(tmp_path: Path) -> None:
    catalog = CatalogSnapshot(
        source_commit="f" * 40,
        patch="16.13.1",
        skins=(
            record("Lux", "Élémentalist Lux K_DA", 4096),
            record("Ahri", "Star Guardian Ahri", 2048),
        ),
    )
    opened: list[Path] = []
    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: catalog,
        path_opener=opened.append,
    )
    _root, tree = attach_presenter_fakes(app)

    app._load_catalog_now()
    assert app._stats_var.get() == "2 skins  •  2 champions  •  Patch 16.13.1  •  ffffffff"
    assert app._champion_box.values == (app.ALL_CHAMPIONS, "Ahri", "Lux")
    assert [values[0] for values in tree.items.values()] == ["Ahri", "Lux"]

    app._search_var.set("element kda")
    app._apply_filter()
    assert list(tree.items.values()) == [("Lux", "Élémentalist Lux K_DA", "4.0 KB")]
    assert app._result_var.get() == "1 result"

    app._search_var.set("")
    app._champion_var.set("Ahri")
    app._apply_filter()
    assert list(tree.items.values())[0][1] == "Star Guardian Ahri"

    app._champion_var.set(app.ALL_CHAMPIONS)
    app._sort_by("size")
    app._sort_by("size")
    assert list(tree.items.values())[0][1] == "Élémentalist Lux K_DA"
    tree.selected = ("0",)
    app._selection_changed()
    assert "Élémentalist Lux K_DA" in app._detail_var.get()
    app._open_selected()
    assert opened == [tmp_path / "installed" / catalog.skins[0].directory]


def test_presenter_queue_marshals_status_visibility_refresh_and_stop(tmp_path: Path) -> None:
    loads: list[Path] = []
    app = make_desktop(
        tmp_path,
        catalog_loader=lambda path: loads.append(path) or CatalogSnapshot("", None, ()),
    )
    root, _tree = attach_presenter_fakes(app)

    app.show()
    app.hide()
    app.refresh_catalog()
    app.update_status(AppState.READY, "Ready - 1,920 skins")
    app.update_ltk_status("migrating 2/10", migration_active=True)
    app._drain_events()

    assert root.actions[:4] == ["show", "lift", "focus", "hide"]
    assert app._status_var.get() == "Ready - 1,920 skins"
    assert app._ltk_status_var.get() == "LTK companion: migrating 2/10"
    assert app._cancel_migration_button.state == "normal"
    assert len(loads) == 2
    assert root.actions[-1] == "after"

    app.stop()
    app._drain_events()
    assert root.actions[-1] == "destroy"


def test_presenter_actions_report_failures_and_preserve_startup_state(tmp_path: Path) -> None:
    opened: list[Path] = []
    calls: list[str] = []

    def fail_open(path: Path) -> None:
        opened.append(path)
        raise OSError("shell unavailable")

    app = make_desktop(
        tmp_path,
        on_sync=lambda: False,
        on_start_manager=lambda: False,
        on_start_ltk=lambda: False,
        on_exit=lambda: calls.append("exit") or True,
        set_startup_enabled=lambda _enabled: False,
        path_opener=fail_open,
    )
    root, tree = attach_presenter_fakes(app)

    app._sync_clicked()
    assert app._status_var.get() == "Sync was not started"
    app._manager_clicked()
    assert app._status_var.get() == "CSLOL Manager could not be started"
    app._ltk_clicked()
    assert app._ltk_status_var.get() == "LTK companion: launch was not started"
    app._startup_var.set(True)
    app._startup_clicked()
    assert app._startup_var.get() is False
    app._open_path(tmp_path)
    assert "Could not open" in app._status_var.get()
    tree.selected = ()
    app._open_selected()
    assert app._status_var.get() == "Select a skin first"

    app._filter_after_id = "old-filter"
    app._filter_changed()
    assert root.cancelled == ["old-filter"]
    app._hide_now()
    app._exit_clicked()
    finish_exit_request(app)
    assert calls == ["exit"]
    assert root.actions[-2:] == ["hide", "destroy"]


def test_tray_migration_request_is_selected_and_confirmed_on_tk_thread(tmp_path: Path) -> None:
    selected = tmp_path / "cslol-manager"
    migrated: list[Path] = []
    confirmations: list[Path] = []
    app = make_desktop(
        tmp_path,
        directory_selector=lambda initial: selected if initial.name == "installed" else None,
        migration_confirmation=lambda path: confirmations.append(path) or True,
        on_migrate_to_ltk=lambda path: migrated.append(path) or True,
    )
    root, _tree = attach_presenter_fakes(app)

    app.request_ltk_migration()
    app._drain_events()

    assert root.actions[:3] == ["show", "lift", "focus"]
    assert confirmations == [selected]
    assert migrated == [selected]
    assert app._ltk_status_var.get() == "LTK companion: migration queued"
    assert app._cancel_migration_button.state == "normal"


def test_cancel_migration_reports_request_and_declined_confirmation_does_nothing(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    app = make_desktop(
        tmp_path,
        directory_selector=lambda _initial: tmp_path / "manager",
        migration_confirmation=lambda _path: False,
        on_migrate_to_ltk=lambda _path: calls.append("migrate") or True,
        on_cancel_ltk_migration=lambda: calls.append("cancel") or True,
    )
    attach_presenter_fakes(app)

    app._migration_clicked()
    app._cancel_migration_clicked()

    assert calls == ["cancel"]
    assert app._ltk_status_var.get() == "LTK companion: cancelling migration safely..."


def test_reset_history_requires_confirmation_and_reports_queue_result(tmp_path: Path) -> None:
    resets: list[str] = []
    confirmation = {"allowed": False}
    app = make_desktop(
        tmp_path,
        history_reset_confirmation=lambda: confirmation["allowed"],
        on_reset_ltk_migration=lambda: resets.append("reset") or True,
    )
    attach_presenter_fakes(app)

    app._reset_migration_history_clicked()
    assert resets == []

    confirmation["allowed"] = True
    app._reset_migration_history_clicked()
    assert resets == ["reset"]
    assert app._ltk_status_var.get() == "LTK companion: migration-history reset queued"


def test_desktop_exit_keeps_ui_responsive_while_shutdown_waits(tmp_path: Path) -> None:
    started = Event()
    release = Event()

    def stop_application() -> bool:
        started.set()
        assert release.wait(1.0)
        return True

    app = make_desktop(tmp_path, on_exit=stop_application)
    root, _tree = attach_presenter_fakes(app)

    app._exit_clicked()

    assert started.wait(1.0)
    assert app._status_var.get() == "Stopping application…"
    assert "destroy" not in root.actions
    app._exit_clicked()
    release.set()
    finish_exit_request(app)

    assert root.actions[-1] == "destroy"


def test_failed_async_exit_allows_a_retry(tmp_path: Path) -> None:
    results = iter((False, True))
    app = make_desktop(tmp_path, on_exit=lambda: next(results))
    root, _tree = attach_presenter_fakes(app)

    app._exit_clicked()
    finish_exit_request(app)

    assert app._status_var.get() == "Background work is still stopping; try Exit again"
    assert "destroy" not in root.actions

    app._exit_clicked()
    finish_exit_request(app)

    assert root.actions[-1] == "destroy"


def test_catalog_error_is_presented_without_replacing_existing_rows(tmp_path: Path) -> None:
    app = make_desktop(
        tmp_path,
        catalog_loader=lambda _path: (_ for _ in ()).throw(CatalogError("broken catalog")),
    )
    _root, tree = attach_presenter_fakes(app)
    tree.items["keep"] = ("Ahri", "Keep", "1 KB")

    app._load_catalog_now()

    assert app._status_var.get() == "broken catalog"
    assert "keep" in tree.items
