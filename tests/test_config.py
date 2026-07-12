from __future__ import annotations

import logging
from pathlib import Path

import pytest

from league_skin_manager.atomic import read_json
from league_skin_manager.config import APP_NAME, AppPaths, RuntimeConfig
from league_skin_manager.logging_setup import configure_logging


def test_paths_are_side_effect_free_until_ensured(tmp_path: Path) -> None:
    appdata = tmp_path / "roaming"
    project = tmp_path / "project"
    paths = AppPaths.discover(appdata=appdata, project_root=project)

    assert paths.data_dir == appdata.resolve() / APP_NAME
    assert paths.installed_dir == paths.manager_dir / "installed"
    assert not paths.data_dir.exists()

    paths.ensure()
    assert paths.installed_dir.is_dir()
    assert paths.package_cache_dir.is_dir()
    assert paths.log_dir.is_dir()


def test_runtime_config_rejects_unsafe_values() -> None:
    with pytest.raises(ValueError, match="positive"):
        RuntimeConfig(process_poll_seconds=0)
    with pytest.raises(ValueError, match="between"):
        RuntimeConfig(download_workers=17)
    with pytest.raises(ValueError, match="positive"):
        RuntimeConfig(download_attempts=0)


def test_read_json_uses_fallback_for_malformed_content(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("not-json", encoding="utf-8")

    assert read_json(path, {"fallback": True}) == {"fallback": True}


def test_logging_configuration_is_idempotent(tmp_path: Path) -> None:
    logger = logging.getLogger("league_skin_manager")
    logger.handlers.clear()
    first = configure_logging(tmp_path)
    second = configure_logging(tmp_path)
    assert first is second
    assert len(first.handlers) == 2
