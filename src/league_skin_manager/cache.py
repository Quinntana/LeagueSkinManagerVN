"""Content-addressed store for verified skin packages.

Every file is named by its Git blob SHA, so the filename *is* the content's
identity.  That is the whole design: there is no index, no manifest, and no
recorded mapping between "what we downloaded" and "what is on disk", so
nothing can drift out of agreement with reality.

The previous design's 743 KB manifest and two ~600 KB digest indexes existed
to maintain exactly that mapping, and their disagreements with reality were
the source of its corruption-recovery machinery.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .github import SkinAsset, SkinManifest
from .hashing import is_real_file

LOGGER = logging.getLogger(__name__)

SUFFIX = ".fantome"


@dataclass(frozen=True, slots=True)
class CacheStatus:
    """What a cache holds relative to a manifest."""

    present: tuple[SkinAsset, ...]
    missing: tuple[SkinAsset, ...]

    @property
    def is_complete(self) -> bool:
        return not self.missing


class PackageCache:
    """A directory of verified packages named by Git blob SHA."""

    def __init__(self, directory: Path, logger: logging.Logger = LOGGER) -> None:
        self.directory = Path(directory)
        self.logger = logger

    def ensure(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, asset: SkinAsset) -> Path:
        """Return where *asset* lives, present or not."""

        return self.directory / f"{asset.sha}{SUFFIX}"

    def holds(self, asset: SkinAsset) -> bool:
        """Return whether the cache already holds *asset*.

        Name and size only.  The name is the content digest and nothing is
        written under it until it has been verified, so rehashing here would
        re-prove something already established at download time.
        """

        path = self.path_for(asset)
        if not is_real_file(path):
            return False
        try:
            return path.stat().st_size == asset.size
        except OSError:
            return False

    def status(self, manifest: SkinManifest) -> CacheStatus:
        """Split *manifest* into what is cached and what still has to be fetched."""

        present: list[SkinAsset] = []
        missing: list[SkinAsset] = []
        for asset in manifest.assets:
            (present if self.holds(asset) else missing).append(asset)
        return CacheStatus(tuple(present), tuple(missing))

    def discard(self, asset: SkinAsset) -> None:
        """Remove a package that failed validation."""

        try:
            self.path_for(asset).unlink(missing_ok=True)
        except OSError:
            self.logger.warning("Could not discard invalid package %s", asset.path, exc_info=True)

    def prune(self, keep: Iterable[SkinAsset]) -> int:
        """Delete cached files not named by any asset in *keep*.

        Upstream renames and removals would otherwise accumulate dead blobs
        across years of patches.
        """

        wanted = {f"{asset.sha}{SUFFIX}" for asset in keep}
        removed = 0
        try:
            entries = tuple(self.directory.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.name in wanted or not is_real_file(entry):
                continue
            try:
                entry.unlink()
                removed += 1
            except OSError:
                self.logger.warning("Could not prune cache entry %s", entry.name, exc_info=True)
        if removed:
            self.logger.info("Pruned %d cache entries no longer in the manifest", removed)
        return removed


__all__ = ["SUFFIX", "CacheStatus", "PackageCache"]
