from __future__ import annotations

import json
from pathlib import Path

import pytest

from league_skin_manager.catalog import CatalogError, load_catalog
from league_skin_manager.skin_installer import managed_directory_name


def write_catalog(path: Path) -> None:
    entries = []
    for champion, name, size, sha in (
        ("Ahri", "Arcana Ahri", 1200, "a" * 40),
        ("Ahri", "Star Guardian Ahri", 2400, "b" * 40),
        ("Lux", "Élémentalist Lux K_DA", 3600, "c" * 40),
    ):
        source_path = f"skins/{champion}/{name}.fantome"
        entries.append(
            {
                "champion": champion,
                "name": name,
                "source_path": source_path,
                "source_sha": sha,
                "size": size,
                "directory": managed_directory_name(champion, name, source_path),
                "content_sha256": "d" * 64,
            }
        )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "transaction_id": "transaction",
                "source_commit": "e" * 40,
                "patch": "16.13.1",
                "entries": entries,
            }
        ),
        encoding="utf-8",
    )


def test_catalog_loads_stats_and_searches_all_terms(tmp_path: Path) -> None:
    path = tmp_path / "managed_skins.json"
    write_catalog(path)

    catalog = load_catalog(path)

    assert catalog.patch == "16.13.1"
    assert catalog.source_commit == "e" * 40
    assert catalog.champions == ("Ahri", "Lux")
    assert catalog.total_bytes == 7200
    assert [skin.name for skin in catalog.filtered("ahri star")] == ["Star Guardian Ahri"]
    assert [skin.name for skin in catalog.filtered("element kda", "Lux")] == [
        "Élémentalist Lux K_DA"
    ]
    assert catalog.filtered("lux", "Ahri") == ()


def test_missing_catalog_is_an_empty_first_run(tmp_path: Path) -> None:
    catalog = load_catalog(tmp_path / "missing.json")

    assert catalog.skins == ()
    assert catalog.champions == ()
    assert catalog.total_bytes == 0


def test_malformed_or_untrusted_catalog_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "managed_skins.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(CatalogError, match="unreadable"):
        load_catalog(path)
