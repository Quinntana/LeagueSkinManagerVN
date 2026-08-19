"""Wipe and reseed LTK Manager's skin library.

The contract implemented here was established by experiment against LTK
Manager v1.13.0, not inferred from its source:

* LTK reconciles its library from disk on every startup, unconditionally.  The
  ``watcherEnabled`` setting governs only the live watcher, so a library
  mutated while LTK is closed is repaired the next time it starts.
* A stale ``library.json`` referencing 1,922 now-absent mods is repaired
  automatically ("Removing orphaned mod entry ... files missing from disk"),
  so this module never reads or writes it.
* A package dropped into ``archives/`` under any filename is adopted, copied
  to ``<uuid>.fantome``, and registered.  The dropped file is then deleted by
  LTK, so names here need only be unique.
* Adopted packages land *enabled*, so no activation step is needed.
* Mutating the directories while LTK runs is safe but incomplete: deletions
  are noticed, additions are not adopted until restart.

The write surface is therefore two directories.  ``library.json``,
``wad-reports.json``, ``profiles/``, and LTK's own files are never touched.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .hashing import is_real_directory

LOGGER = logging.getLogger(__name__)

ARCHIVES = "archives"
MODS = "mods"
PART_SUFFIX = ".part"


class SeedError(RuntimeError):
    """LTK's storage could not be prepared or populated."""


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What one wipe-and-reseed did."""

    removed_archives: int
    removed_mods: int
    seeded: int


def seed_library(storage_dir: Path, packages: Sequence[Path]) -> SeedResult:
    """Replace LTK's skin library with exactly *packages*.

    Indiscriminate by design: anything already in ``archives/`` or ``mods/``
    is removed, including packages imported by hand.  A clean base is what
    makes the result independent of whatever state LTK was left in.
    """

    archives = storage_dir / ARCHIVES
    mods = storage_dir / MODS
    _prepare(storage_dir, archives, mods)

    removed_archives = _empty_directory(archives)
    removed_mods = _empty_directory(mods)

    seeded = 0
    for index, source in enumerate(packages):
        # Staged under a .part name so a partially copied file is never a
        # candidate for adoption, even if LTK happens to be running.
        staged = archives / f"lsmvn-{index:05d}.fantome{PART_SUFFIX}"
        final = archives / f"lsmvn-{index:05d}.fantome"
        try:
            shutil.copyfile(source, staged)
            staged.replace(final)
        except OSError as error:
            staged.unlink(missing_ok=True)
            raise SeedError(f"Could not stage package into LTK storage: {source.name}") from error
        seeded += 1

    LOGGER.info(
        "Reseeded LTK library: removed %d archives and %d mod directories, seeded %d packages",
        removed_archives,
        removed_mods,
        seeded,
    )
    return SeedResult(removed_archives, removed_mods, seeded)


def _prepare(storage_dir: Path, archives: Path, mods: Path) -> None:
    """Create the two owned directories, refusing anything that is not a real one."""

    for path in (storage_dir, archives, mods):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise SeedError(f"Could not create LTK storage directory: {path}") from error
        if not is_real_directory(path):
            raise SeedError(f"LTK storage path is not a real directory: {path}")


def _empty_directory(path: Path) -> int:
    """Delete every entry in *path*, returning how many were removed."""

    removed = 0
    try:
        entries = tuple(path.iterdir())
    except OSError as error:
        raise SeedError(f"Could not read LTK storage directory: {path}") from error
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError as error:
            raise SeedError(f"Could not remove LTK library entry: {entry.name}") from error
    return removed


__all__ = ["ARCHIVES", "MODS", "SeedError", "SeedResult", "seed_library"]
