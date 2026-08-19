"""Persisted application settings.

This one small file replaces the previous design's entire persisted state: a
743 KB install manifest and two ~600 KB digest indexes.  It can afford to be
this small because nothing here is a ledger.  The skin set is identified by
the upstream commit alone, and the package cache is content-addressed, so
there is no mapping between "what we installed" and "what is on disk" that
could ever disagree with reality.

Reading is deliberately forgiving.  This is our own file describing
reconstructible state; a corrupt or partial one costs a re-sync, so an
unreadable value falls back to its default rather than failing the launch.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, read_json

LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# Presets offered in the tray. The panel is sized for a 1080p client, so the
# choices run mostly smaller with a single larger option.
OPACITY_CHOICES: tuple[float, ...] = (1.0, 0.85, 0.70, 0.55)
SCALE_CHOICES: tuple[float, ...] = (0.70, 0.85, 1.0, 1.25)

DEFAULT_OPACITY = 0.85
DEFAULT_SCALE = 1.0


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything this application remembers between launches."""

    # --- skin sync state -------------------------------------------------
    commit: str | None = None
    """Upstream commit of the last completed sync. None means never synced.

    Written only after every package has landed, which is what makes an
    interrupted sync self-healing: a missing or stale value simply redoes it.
    """

    patch: str | None = None
    skins: int = 0
    synced_at: str | None = None

    # --- ownership -------------------------------------------------------
    ltk_installed_by_app: bool = False
    """Whether this application installed LTK Manager.

    Recorded at install time because it cannot be derived later, and it decides
    two things: whether uninstall removes LTK, and whether we refuse to touch
    a library that was already there.
    """

    # --- cooldown panel --------------------------------------------------
    cooldown_auto_run: bool = False
    cooldown_opacity: float = DEFAULT_OPACITY
    cooldown_scale: float = DEFAULT_SCALE
    cooldown_left: int | None = None
    cooldown_top: int | None = None

    def with_sync(self, *, commit: str, patch: str | None, skins: int, synced_at: str) -> Settings:
        """Return a copy recording a completed sync."""

        return replace(self, commit=commit, patch=patch, skins=skins, synced_at=synced_at)

    def with_display(self, *, opacity: float | None = None, scale: float | None = None) -> Settings:
        """Return a copy with validated cooldown display settings."""

        return replace(
            self,
            cooldown_opacity=_nearest(opacity, OPACITY_CHOICES, self.cooldown_opacity),
            cooldown_scale=_nearest(scale, SCALE_CHOICES, self.cooldown_scale),
        )


def load(path: Path) -> Settings:
    """Read settings from *path*, falling back to defaults for anything unusable."""

    raw = read_json(path, default=None)
    if not isinstance(raw, dict):
        if raw is not None:
            LOGGER.warning("Settings file is not an object; using defaults: %s", path)
        return Settings()

    defaults = Settings()
    return Settings(
        commit=_text(raw.get("commit")),
        patch=_text(raw.get("patch")),
        skins=_count(raw.get("skins"), defaults.skins),
        synced_at=_text(raw.get("synced_at")),
        ltk_installed_by_app=_flag(raw.get("ltk_installed_by_app"), defaults.ltk_installed_by_app),
        cooldown_auto_run=_flag(raw.get("cooldown_auto_run"), defaults.cooldown_auto_run),
        cooldown_opacity=_nearest(
            _number(raw.get("cooldown_opacity")), OPACITY_CHOICES, DEFAULT_OPACITY
        ),
        cooldown_scale=_nearest(_number(raw.get("cooldown_scale")), SCALE_CHOICES, DEFAULT_SCALE),
        cooldown_left=_coordinate(raw.get("cooldown_left")),
        cooldown_top=_coordinate(raw.get("cooldown_top")),
    )


def save(path: Path, settings: Settings) -> None:
    """Write *settings* to *path* atomically."""

    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    payload.update(asdict(settings))
    atomic_write_json(path, payload)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _flag(value: object, fallback: bool) -> bool:
    return value if isinstance(value, bool) else fallback


def _count(value: object, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return fallback
    return value


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _coordinate(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _nearest(value: float | None, choices: tuple[float, ...], fallback: float) -> float:
    """Snap *value* to the closest offered preset.

    Presets are the only values the tray can produce, but a hand-edited file
    may hold anything. Snapping keeps the tray's radio buttons in a consistent
    state instead of showing nothing selected.
    """

    if value is None:
        return fallback
    return min(choices, key=lambda choice: abs(choice - value))


__all__ = [
    "DEFAULT_OPACITY",
    "DEFAULT_SCALE",
    "OPACITY_CHOICES",
    "SCALE_CHOICES",
    "SCHEMA_VERSION",
    "Settings",
    "load",
    "save",
]
