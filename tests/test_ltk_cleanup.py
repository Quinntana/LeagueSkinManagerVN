from __future__ import annotations

import json
from pathlib import Path

import pytest

import league_skin_manager.ltk_cleanup as cleanup_module
from league_skin_manager.ltk_cleanup import (
    LtkSkinCleanupBlockedError,
    LtkSkinCleanupBusyError,
    LtkSkinCleanupError,
    LtkSkinCleanupService,
)


def write_library(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "mods": [
                    {"id": "one", "format": "fantome"},
                    {"id": "two", "format": "modpkg"},
                ],
                "profiles": [
                    {
                        "id": "profile-id",
                        "name": "My profile",
                        "enabledMods": ["one", "two"],
                        "modOrder": ["two", "one"],
                        "layerStates": {"one": {"base": False}},
                    }
                ],
                "activeProfileId": "profile-id",
                "folders": [
                    {"id": "root", "name": "", "modIds": ["one", "two"]},
                    {"id": "favorites", "name": "Favorites", "modIds": ["two"]},
                ],
                "folderOrder": ["root", "favorites"],
            }
        ),
        encoding="utf-8",
    )


def populated_storage(tmp_path: Path) -> Path:
    storage = tmp_path / "ltk-storage"
    archives = storage / "archives"
    mods = storage / "mods"
    profiles = storage / "profiles"
    archives.mkdir(parents=True)
    mods.mkdir()
    profiles.mkdir()
    (archives / "one.fantome").write_bytes(b"one")
    (archives / "two.modpkg").write_bytes(b"two")
    (mods / "one").mkdir()
    (mods / "one" / "mod.config.json").write_text("{}", encoding="utf-8")
    (mods / "two").mkdir()
    (profiles / "default").mkdir()
    (profiles / "default" / "game_index.bin").write_bytes(b"index")
    (profiles / "default" / "overlay").mkdir()
    (profiles / "default" / "overlay" / "data.wad").write_bytes(b"wad")
    write_library(storage / "library.json")
    (storage / "wad-reports.json").write_text('{"reports": {}}', encoding="utf-8")
    (storage / "settings.json").write_text('{"theme": "system"}', encoding="utf-8")
    (storage / ".window-state.json").write_text("{}", encoding="utf-8")
    (storage / "logs").mkdir()
    (storage / "logs" / "ltk.log").write_text("keep", encoding="utf-8")
    return storage


def test_remove_all_clears_full_skin_state_and_preserves_ltk_preferences(
    tmp_path: Path,
) -> None:
    storage = populated_storage(tmp_path)
    service = LtkSkinCleanupService(lambda: storage, ltk_is_running=lambda: False)

    result = service.remove_all()

    assert result.storage_dir == storage.resolve()
    assert result.library_mods == 2
    assert result.archives == 2
    assert result.metadata_directories == 2
    assert result.profile_directories == 1
    assert result.removed_items == 5
    assert result.reports_removed
    assert result.library_reset
    assert not (storage / "archives").exists()
    assert not (storage / "mods").exists()
    assert not (storage / "profiles").exists()
    assert not (storage / "wad-reports.json").exists()

    library = json.loads((storage / "library.json").read_text(encoding="utf-8"))
    assert library["mods"] == []
    assert library["profiles"][0]["name"] == "My profile"
    assert library["profiles"][0]["enabledMods"] == []
    assert library["profiles"][0]["modOrder"] == []
    assert library["profiles"][0]["layerStates"] == {}
    assert [folder["name"] for folder in library["folders"]] == ["", "Favorites"]
    assert all(folder["modIds"] == [] for folder in library["folders"])
    assert library["folderOrder"] == ["root", "favorites"]

    assert (storage / "settings.json").read_text(encoding="utf-8") == '{"theme": "system"}'
    assert (storage / ".window-state.json").exists()
    assert (storage / "logs" / "ltk.log").read_text(encoding="utf-8") == "keep"


def test_missing_storage_is_a_successful_noop(tmp_path: Path) -> None:
    storage = tmp_path / "missing"
    result = LtkSkinCleanupService(
        lambda: storage,
        ltk_is_running=lambda: False,
    ).remove_all()

    assert result.storage_dir == storage.resolve()
    assert result.removed_items == 0
    assert not result.reports_removed
    assert not result.library_reset
    assert not storage.exists()


def test_running_or_unverifiable_ltk_blocks_before_mutation(tmp_path: Path) -> None:
    storage = populated_storage(tmp_path)
    service = LtkSkinCleanupService(lambda: storage, ltk_is_running=lambda: True)

    with pytest.raises(LtkSkinCleanupBlockedError, match="Close LTK Manager"):
        service.remove_all()

    assert (storage / "archives" / "one.fantome").exists()

    broken = LtkSkinCleanupService(
        lambda: storage,
        ltk_is_running=lambda: (_ for _ in ()).throw(OSError("process denied")),
    )
    with pytest.raises(LtkSkinCleanupBlockedError, match="verify"):
        broken.remove_all()

    non_boolean = LtkSkinCleanupService(
        lambda: storage,
        ltk_is_running=lambda: None,  # type: ignore[arg-type,return-value]
    )
    with pytest.raises(LtkSkinCleanupBlockedError, match="verify"):
        non_boolean.remove_all()


def test_second_process_check_catches_ltk_starting_during_preflight(tmp_path: Path) -> None:
    storage = populated_storage(tmp_path)
    checks = 0

    def starts_during_preflight() -> bool:
        nonlocal checks
        checks += 1
        return checks == 2

    service = LtkSkinCleanupService(
        lambda: storage,
        ltk_is_running=starts_during_preflight,
    )

    with pytest.raises(LtkSkinCleanupBlockedError, match="Close LTK Manager"):
        service.remove_all()

    assert checks == 2
    assert (storage / "archives" / "one.fantome").exists()
    assert json.loads((storage / "library.json").read_text(encoding="utf-8"))["mods"]


def test_unsafe_target_aborts_everything_before_deletion(tmp_path: Path) -> None:
    storage = populated_storage(tmp_path)
    reports = storage / "wad-reports.json"
    reports.unlink()
    reports.mkdir()

    service = LtkSkinCleanupService(lambda: storage, ltk_is_running=lambda: False)
    with pytest.raises(LtkSkinCleanupError, match="not a normal file"):
        service.remove_all()

    assert (storage / "archives" / "one.fantome").exists()
    assert (storage / "mods" / "one").exists()


def test_reparse_storage_root_is_rejected_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = populated_storage(tmp_path)
    original = cleanup_module._is_reparse_point
    monkeypatch.setattr(
        cleanup_module,
        "_is_reparse_point",
        lambda path: path == storage or original(path),
    )

    service = LtkSkinCleanupService(lambda: storage, ltk_is_running=lambda: False)
    with pytest.raises(LtkSkinCleanupError, match="not a normal directory"):
        service.remove_all()

    assert (storage / "archives" / "one.fantome").exists()


def test_malformed_library_is_removed_so_ltk_can_recreate_defaults(tmp_path: Path) -> None:
    storage = populated_storage(tmp_path)
    (storage / "library.json").write_text("not json", encoding="utf-8")

    result = LtkSkinCleanupService(
        lambda: storage,
        ltk_is_running=lambda: False,
    ).remove_all()

    assert result.library_mods == 0
    assert result.library_reset
    assert not (storage / "library.json").exists()
    assert not (storage / "archives").exists()


def test_concurrent_cleanup_and_invalid_limit_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        LtkSkinCleanupService(lambda: tmp_path, ltk_is_running=lambda: False, max_library_bytes=0)

    service = LtkSkinCleanupService(lambda: tmp_path, ltk_is_running=lambda: False)
    assert service._lock.acquire(blocking=False)
    try:
        with pytest.raises(LtkSkinCleanupBusyError, match="already in progress"):
            service.remove_all()
    finally:
        service._lock.release()
