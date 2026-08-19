"""Read-only inspection of LTK Manager's mod library index.

This module never writes.  It exists so the tray can report the honest
end-to-end state - how many skins LTK actually holds and how many are switched
on - without any component that mutates LTK having to be involved.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

LTK_LIBRARY_FILENAME = "library.json"
MAX_LIBRARY_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class LtkLibrarySummary:
    """How many mods LTK holds, and how many the active profile enables."""

    in_library: int
    enabled: int


def summarize_library(storage_dir: Path) -> LtkLibrarySummary | None:
    """Return LTK's library counts, or None when they cannot be trusted.

    Returning None covers every "we do not know" case - no library yet, an
    unsafe or oversized file, malformed JSON - so the caller can say so
    plainly instead of reporting a misleading zero.
    """

    path = Path(storage_dir) / LTK_LIBRARY_FILENAME
    raw = _read_library(path)
    if raw is None:
        return None
    mods = raw.get("mods")
    if not isinstance(mods, list):
        return None
    identifiers = {
        mod["id"] for mod in mods if isinstance(mod, Mapping) and isinstance(mod.get("id"), str)
    }
    return LtkLibrarySummary(
        in_library=len(mods),
        enabled=_count_enabled(raw, identifiers),
    )


def _count_enabled(raw: Mapping[str, object], identifiers: set[str]) -> int:
    """Count enabled mods in the active profile, ignoring stale references."""

    profiles = raw.get("profiles")
    if not isinstance(profiles, list):
        return 0
    active_id = raw.get("activeProfileId")
    active: Mapping[str, object] | None = None
    for profile in profiles:
        if not isinstance(profile, Mapping):
            continue
        if active is None:
            active = profile
        if isinstance(active_id, str) and profile.get("id") == active_id:
            active = profile
            break
    if active is None:
        return 0
    enabled = active.get("enabledMods")
    if not isinstance(enabled, list):
        return 0
    return sum(1 for value in enabled if isinstance(value, str) and value in identifiers)


def _read_library(path: Path) -> Mapping[str, object] | None:
    try:
        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return None
        if info.st_size > MAX_LIBRARY_BYTES:
            return None
        raw = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return raw if isinstance(raw, Mapping) else None


__all__ = ["MAX_LIBRARY_BYTES", "LtkLibrarySummary", "summarize_library"]
