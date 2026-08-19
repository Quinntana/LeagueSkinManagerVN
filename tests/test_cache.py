"""Tests for the content-addressed package cache."""

from __future__ import annotations

from pathlib import Path

from league_skin_manager.cache import PackageCache
from league_skin_manager.github import SkinAsset, SkinManifest


def asset(sha_seed: int, size: int = 32, champion: str = "Ahri") -> SkinAsset:
    sha = f"{sha_seed:040x}"
    name = f"Skin{sha_seed}"
    return SkinAsset(
        champion=champion,
        name=name,
        path=f"skins/{champion}/{name}.fantome",
        size=size,
        sha=sha,
    )


def manifest(*assets: SkinAsset) -> SkinManifest:
    return SkinManifest(commit="a" * 40, patch="16.15.1", assets=assets)


def store(cache: PackageCache, item: SkinAsset, *, size: int | None = None) -> Path:
    cache.ensure()
    path = cache.path_for(item)
    path.write_bytes(b"x" * (item.size if size is None else size))
    return path


def test_a_package_is_named_by_its_blob_sha(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    item = asset(1)
    assert cache.path_for(item).name == f"{item.sha}.fantome"


def test_an_absent_package_is_not_held(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    assert not cache.holds(asset(1))


def test_a_stored_package_is_held(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    item = asset(1)
    store(cache, item)
    assert cache.holds(item)


def test_a_wrong_sized_package_is_not_held(tmp_path: Path) -> None:
    """Truncation is the failure a name alone cannot detect."""

    cache = PackageCache(tmp_path)
    item = asset(1, size=64)
    store(cache, item, size=10)
    assert not cache.holds(item)


def test_a_directory_is_not_mistaken_for_a_package(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    item = asset(1)
    cache.ensure()
    cache.path_for(item).mkdir()
    assert not cache.holds(item)


def test_status_splits_present_from_missing(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    here, gone = asset(1), asset(2)
    store(cache, here)

    status = cache.status(manifest(here, gone))

    assert status.present == (here,)
    assert status.missing == (gone,)
    assert not status.is_complete


def test_a_fully_cached_manifest_is_complete(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    items = [asset(index) for index in range(5)]
    for item in items:
        store(cache, item)

    status = cache.status(manifest(*items))

    assert status.is_complete
    assert status.missing == ()
    assert len(status.present) == 5


def test_discard_removes_an_invalid_package(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    item = asset(1)
    store(cache, item)
    cache.discard(item)
    assert not cache.path_for(item).exists()


def test_discarding_an_absent_package_is_harmless(tmp_path: Path) -> None:
    PackageCache(tmp_path).discard(asset(1))


def test_prune_removes_entries_not_in_the_manifest(tmp_path: Path) -> None:
    """Upstream renames would otherwise accumulate dead blobs forever."""

    cache = PackageCache(tmp_path)
    keep, drop = asset(1), asset(2)
    store(cache, keep)
    store(cache, drop)

    removed = cache.prune([keep])

    assert removed == 1
    assert cache.path_for(keep).is_file()
    assert not cache.path_for(drop).exists()


def test_prune_keeps_everything_still_wanted(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path)
    items = [asset(index) for index in range(4)]
    for item in items:
        store(cache, item)

    assert cache.prune(items) == 0
    assert len(list(tmp_path.iterdir())) == 4


def test_prune_on_a_missing_directory_is_harmless(tmp_path: Path) -> None:
    assert PackageCache(tmp_path / "absent").prune([asset(1)]) == 0


def test_ensure_creates_the_directory(tmp_path: Path) -> None:
    cache = PackageCache(tmp_path / "nested" / "cache")
    cache.ensure()
    assert cache.directory.is_dir()
