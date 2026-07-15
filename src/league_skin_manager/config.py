"""Side-effect-free application configuration and path discovery."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "LeagueSkinManagerVN"
APP_DISPLAY_NAME = "League Skin Manager VN"
APP_VERSION = "2.5.0"
APP_PUBLISHER = "Quinntana"
APP_INFO_URL = "https://github.com/Quinntana/LeagueSkinManagerVN"
UNINSTALL_APP_NAME = "LeagueSkinManagerVNUninstall"
SETUP_APP_NAME = "LeagueSkinManagerVNSetup"
MANAGER_PROCESS_NAME = "cslol-manager.exe"
MANAGER_PROCESS_NAMES = (MANAGER_PROCESS_NAME, "mod-tools.exe")
LTK_PROCESS_NAMES = (
    "ltk-manager.exe",
    "LTK Manager.exe",
    "ltk_patcher_host.exe",
    "LeagueSkinManagerLTK.exe",
)
LEAGUE_PROCESS_NAME = "LeagueClient.exe"

CSLOL_RELEASES_URL = "https://api.github.com/repos/LeagueToolkit/cslol-manager/releases/latest"
LTK_RELEASES_URL = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"
SKIN_SOURCE_OWNER = "bettie9"
SKIN_SOURCE_REPOSITORY = "LeagueSkins"
SKIN_SOURCE_BRANCH = "main"


@dataclass(frozen=True, slots=True)
class AppPaths:
    project_root: Path
    data_dir: Path
    manager_dir: Path
    installed_dir: Path
    profiles_dir: Path
    cache_dir: Path
    package_cache_dir: Path
    ltk_cache_dir: Path
    migration_report_dir: Path
    ltk_migration_state_file: Path
    ltk_data_dir: Path
    log_dir: Path
    managed_manifest_file: Path
    manager_version_file: Path

    @classmethod
    def discover(
        cls,
        *,
        appdata: str | Path | None = None,
        project_root: str | Path | None = None,
    ) -> AppPaths:
        if project_root is None:
            if getattr(sys, "frozen", False):
                root = Path(sys.executable).resolve().parent
            else:
                root = Path(__file__).resolve().parents[2]
        else:
            root = Path(project_root).resolve()

        appdata_value = appdata if appdata is not None else os.environ.get("APPDATA")
        data_dir = Path(appdata_value).resolve() / APP_NAME if appdata_value else root / "data"
        manager_dir = data_dir / "cslol-manager"
        cache_dir = data_dir / "cache"
        return cls(
            project_root=root,
            data_dir=data_dir,
            manager_dir=manager_dir,
            installed_dir=manager_dir / "installed",
            profiles_dir=manager_dir / "profiles",
            cache_dir=cache_dir,
            package_cache_dir=cache_dir / "packages",
            ltk_cache_dir=cache_dir / "ltk",
            migration_report_dir=data_dir / "migration-reports",
            ltk_migration_state_file=data_dir / "ltk_migration_state.json",
            ltk_data_dir=data_dir.parent / "dev.leaguetoolkit.manager",
            log_dir=data_dir / "logs",
            managed_manifest_file=data_dir / "managed_skins.json",
            manager_version_file=manager_dir / "version.txt",
        )

    def ensure(self) -> None:
        for directory in (
            self.data_dir,
            self.manager_dir,
            self.installed_dir,
            self.profiles_dir,
            self.cache_dir,
            self.package_cache_dir,
            self.ltk_cache_dir,
            self.migration_report_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    process_poll_seconds: float = 5.0
    download_workers: int = 6
    download_attempts: int = 3
    shutdown_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.process_poll_seconds <= 0:
            raise ValueError("process_poll_seconds must be positive")
        if not 1 <= self.download_workers <= 16:
            raise ValueError("download_workers must be between 1 and 16")
        if self.download_attempts < 1:
            raise ValueError("download_attempts must be positive")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")
