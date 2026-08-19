"""Side-effect-free application configuration and path discovery."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "LeagueSkinManagerVN"
APP_DISPLAY_NAME = "League Skin Manager VN"
APP_VERSION = "4.0.0"
APP_PUBLISHER = "Quinntana"
APP_INFO_URL = "https://github.com/Quinntana/LeagueSkinManagerVN"

LTK_PROCESS_NAMES = (
    "ltk-manager.exe",
    "LTK Manager.exe",
    "ltk_patcher_host.exe",
)

# The in-game process, not the client. Only the cooldown panel watches for a
# process at all: LTK starts its own patcher, so nothing here needs to react
# to the League client launching.
LEAGUE_GAME_PROCESS_NAME = "League of Legends.exe"

LTK_RELEASES_URL = "https://api.github.com/repos/LeagueToolkit/ltk-manager/releases/latest"
SKIN_SOURCE_OWNER = "bettie9"
SKIN_SOURCE_REPOSITORY = "LeagueSkins"
SKIN_SOURCE_BRANCH = "main"

# Porofessor is an Overwolf extension with no standalone installer to verify,
# so this application never manages it: the tray opens this page and stops.
POROFESSOR_DOWNLOAD_URL = "https://porofessor.gg/download"

# LTK derives its data root from its Tauri bundle identifier, which is fixed
# per Windows user. An LTK install can relocate mod storage through its own
# modStoragePath setting, so this is only the default starting point.
LTK_BUNDLE_IDENTIFIER = "dev.leaguetoolkit.manager"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Every path this application owns.

    Short because the application owns little: a package cache, an installer
    cache, a settings file, and a log. The previous design also owned a CSLOL
    installation, an extracted mod tree, profiles, a 743 KB manifest, two
    digest indexes, migration reports, and a cooldown event log.
    """

    data_dir: Path
    cache_dir: Path
    package_cache_dir: Path
    ltk_cache_dir: Path
    ltk_data_dir: Path
    log_dir: Path
    settings_file: Path

    @classmethod
    def discover(cls, appdata: str | Path | None = None) -> AppPaths:
        raw = appdata if appdata is not None else os.environ.get("APPDATA")
        if raw:
            root = Path(raw).resolve()
        elif getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
        else:
            root = Path(__file__).resolve().parents[2]

        data_dir = root / APP_NAME if raw else root / "data"
        cache_dir = data_dir / "cache"
        return cls(
            data_dir=data_dir,
            cache_dir=cache_dir,
            package_cache_dir=cache_dir / "packages",
            ltk_cache_dir=cache_dir / "ltk",
            ltk_data_dir=(root if raw else data_dir) / LTK_BUNDLE_IDENTIFIER,
            log_dir=data_dir / "logs",
            settings_file=data_dir / "settings.json",
        )

    def ensure(self) -> None:
        for directory in (
            self.data_dir,
            self.cache_dir,
            self.package_cache_dir,
            self.ltk_cache_dir,
            self.log_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    download_workers: int = 6
    poll_seconds: float = 5.0
    shutdown_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not 1 <= self.download_workers <= 16:
            raise ValueError("download_workers must be between 1 and 16")
        if self.poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        if self.shutdown_timeout_seconds <= 0:
            raise ValueError("shutdown_timeout_seconds must be positive")


__all__ = [
    "APP_DISPLAY_NAME",
    "APP_INFO_URL",
    "APP_NAME",
    "APP_PUBLISHER",
    "APP_VERSION",
    "LEAGUE_GAME_PROCESS_NAME",
    "LTK_BUNDLE_IDENTIFIER",
    "LTK_PROCESS_NAMES",
    "LTK_RELEASES_URL",
    "POROFESSOR_DOWNLOAD_URL",
    "SKIN_SOURCE_BRANCH",
    "SKIN_SOURCE_OWNER",
    "SKIN_SOURCE_REPOSITORY",
    "AppPaths",
    "RuntimeConfig",
]
