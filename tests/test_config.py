"""Tests for configuration and path discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from league_skin_manager.config import (
    APP_NAME,
    LEAGUE_GAME_PROCESS_NAME,
    LTK_BUNDLE_IDENTIFIER,
    LTK_PROCESS_NAMES,
    AppPaths,
    RuntimeConfig,
)


def test_discovery_is_side_effect_free_until_ensured(tmp_path: Path) -> None:
    paths = AppPaths.discover(appdata=tmp_path)
    assert not paths.data_dir.exists()
    assert not paths.log_dir.exists()

    paths.ensure()

    assert paths.data_dir.is_dir()
    assert paths.package_cache_dir.is_dir()
    assert paths.ltk_cache_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = AppPaths.discover(appdata=tmp_path)
    paths.ensure()
    paths.ensure()
    assert paths.data_dir.is_dir()


def test_application_data_lives_under_the_app_name(tmp_path: Path) -> None:
    paths = AppPaths.discover(appdata=tmp_path)
    assert paths.data_dir == tmp_path / APP_NAME
    assert paths.settings_file == paths.data_dir / "settings.json"


def test_ltk_data_is_a_sibling_not_a_child(tmp_path: Path) -> None:
    """LTK's root is its own; we never nest it inside ours."""

    paths = AppPaths.discover(appdata=tmp_path)
    assert paths.ltk_data_dir == tmp_path / LTK_BUNDLE_IDENTIFIER
    assert paths.data_dir not in paths.ltk_data_dir.parents


def test_ltk_storage_is_not_created_by_ensure(tmp_path: Path) -> None:
    """LTK owns its own directory; the seeder creates it only when seeding."""

    paths = AppPaths.discover(appdata=tmp_path)
    paths.ensure()
    assert not paths.ltk_data_dir.exists()


def test_the_cache_holds_packages_and_the_ltk_installer(tmp_path: Path) -> None:
    paths = AppPaths.discover(appdata=tmp_path)
    assert paths.package_cache_dir.parent == paths.cache_dir
    assert paths.ltk_cache_dir.parent == paths.cache_dir


def test_no_cslol_paths_remain(tmp_path: Path) -> None:
    """The manager, installed tree, profiles, manifest and indexes are gone."""

    paths = AppPaths.discover(appdata=tmp_path)
    fields = set(paths.__dataclass_fields__)
    assert not fields & {
        "manager_dir",
        "installed_dir",
        "profiles_dir",
        "manager_version_file",
        "managed_manifest_file",
        "ltk_archive_index_file",
        "ltk_package_index_file",
        "migration_report_dir",
        "cooldown_event_file",
    }


def test_the_watched_process_is_the_game_not_the_client() -> None:
    assert LEAGUE_GAME_PROCESS_NAME == "League of Legends.exe"
    assert LEAGUE_GAME_PROCESS_NAME != "LeagueClient.exe"


def test_ltk_process_names_cover_the_manager_and_its_patcher() -> None:
    assert "ltk-manager.exe" in LTK_PROCESS_NAMES
    assert "ltk_patcher_host.exe" in LTK_PROCESS_NAMES


def test_runtime_defaults_are_valid() -> None:
    config = RuntimeConfig()
    assert 1 <= config.download_workers <= 16
    assert config.poll_seconds > 0


@pytest.mark.parametrize(
    "changes",
    [
        {"download_workers": 0},
        {"download_workers": 17},
        {"poll_seconds": 0},
        {"poll_seconds": -1},
        {"shutdown_timeout_seconds": 0},
    ],
)
def test_runtime_config_rejects_unsafe_values(changes: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RuntimeConfig(**changes)  # type: ignore[arg-type]
