"""The skin synchronization use case.

Five steps, and four of them are skipped on a normal launch:

1. read the branch head (one request)
2. stop if it equals the last completed sync
3. fetch the tree and download whatever the cache lacks
4. wipe LTK's library and reseed it from the cache
5. record the commit -- only now

Recording the commit last is what makes an interrupted sync self-healing.
There is no journal and no rollback: a run that dies leaves the marker unset,
so the next run repeats the whole thing, and repeating it converges from any
starting state because step 4 wipes before it seeds.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .cache import PackageCache
from .fantome import FantomeError, validate_fantome
from .github import CancelSignal, SkinAsset, SkinManifest
from .seed import seed_library
from .settings import Settings

LOGGER = logging.getLogger(__name__)

DEFAULT_WORKERS = 6


class SkinSource(Protocol):
    """The port this use case needs from a skin repository."""

    def head_commit(self) -> str: ...

    def fetch_manifest(self, commit: str | None = ...) -> SkinManifest: ...

    def download(
        self,
        asset: SkinAsset,
        destination: Path,
        commit: str,
        cancel: CancelSignal | None = ...,
    ) -> None: ...


class SyncOutcome(str):
    """Marker strings for what a sync did."""

    UP_TO_DATE = "up-to-date"
    UPDATED = "updated"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class SyncResult:
    outcome: str
    commit: str | None = None
    patch: str | None = None
    seeded: int = 0
    downloaded: int = 0
    rejected: int = 0

    @property
    def changed(self) -> bool:
        return self.outcome == SyncOutcome.UPDATED


def synchronize(
    *,
    source: SkinSource,
    cache: PackageCache,
    settings: Settings,
    storage_dir: Path,
    workers: int = DEFAULT_WORKERS,
    cancel: CancelSignal | None = None,
    on_progress: Callable[[int, int], None] | None = None,
) -> tuple[SyncResult, Settings]:
    """Bring LTK's library in line with the upstream skin set.

    Returns the result and the settings to persist.  Persisting is the
    caller's job so that the commit marker is written only once the caller has
    seen the whole operation succeed.
    """

    head = source.head_commit()
    if settings.commit == head:
        LOGGER.info("Skin source unchanged at %s; nothing to do", head[:12])
        return SyncResult(SyncOutcome.UP_TO_DATE, commit=head, seeded=settings.skins), settings

    LOGGER.info("Skin source moved to %s; synchronizing", head[:12])
    manifest = source.fetch_manifest(head)
    cache.ensure()

    downloaded, rejected = _fill_cache(
        source=source,
        cache=cache,
        manifest=manifest,
        workers=workers,
        cancel=cancel,
        on_progress=on_progress,
    )

    usable = tuple(asset for asset in manifest.assets if cache.holds(asset))
    if not usable:
        raise RuntimeError("No usable skin packages were available to seed")

    result = seed_library(storage_dir, [cache.path_for(asset) for asset in usable])
    cache.prune(usable)

    updated = settings.with_sync(
        commit=head,
        patch=manifest.patch,
        skins=result.seeded,
        synced_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    LOGGER.info(
        "Sync complete: %d seeded, %d downloaded, %d rejected",
        result.seeded,
        downloaded,
        rejected,
    )
    return (
        SyncResult(
            SyncOutcome.UPDATED,
            commit=head,
            patch=manifest.patch,
            seeded=result.seeded,
            downloaded=downloaded,
            rejected=rejected,
        ),
        updated,
    )


def _fill_cache(
    *,
    source: SkinSource,
    cache: PackageCache,
    manifest: SkinManifest,
    workers: int,
    cancel: CancelSignal | None,
    on_progress: Callable[[int, int], None] | None,
) -> tuple[int, int]:
    """Download and validate everything the cache lacks."""

    status = cache.status(manifest)
    if not status.missing:
        return 0, 0

    # Two skins with byte-identical packages share a blob SHA, and therefore a
    # cache path. Fetching both would have two workers writing one file.
    unique: dict[str, SkinAsset] = {}
    for asset in status.missing:
        unique.setdefault(asset.sha, asset)
    pending = tuple(unique.values())

    total = len(pending)
    LOGGER.info("Fetching %d of %d packages", total, len(manifest.assets))
    rejected = 0
    completed = 0

    with ThreadPoolExecutor(
        max_workers=min(workers, total), thread_name_prefix="skin-download"
    ) as pool:
        futures = {
            pool.submit(_fetch_one, source, cache, asset, manifest.commit, cancel): asset
            for asset in pending
        }
        try:
            for future in as_completed(futures):
                if future.result() is False:
                    rejected += 1
                completed += 1
                if on_progress is not None:
                    on_progress(completed, total)
        except BaseException:
            for future in futures:
                future.cancel()
            raise

    if rejected:
        LOGGER.warning("%d packages failed validation and were discarded", rejected)
    return total - rejected, rejected


def _fetch_one(
    source: SkinSource,
    cache: PackageCache,
    asset: SkinAsset,
    commit: str,
    cancel: CancelSignal | None,
) -> bool:
    """Download one package and validate it. Returns False if it was rejected."""

    destination = cache.path_for(asset)
    source.download(asset, destination, commit, cancel)
    try:
        # Full validation happens exactly once, here, before the package is
        # ever considered cached. Later syncs check only name and size.
        validate_fantome(destination, expected_size=asset.size, expected_sha=asset.sha)
    except FantomeError as error:
        LOGGER.info("Rejected %s: %s", asset.path, error)
        cache.discard(asset)
        return False
    return True


__all__ = ["DEFAULT_WORKERS", "SkinSource", "SyncOutcome", "SyncResult", "synchronize"]
