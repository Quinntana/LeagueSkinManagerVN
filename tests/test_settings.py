"""Tests for persisted application settings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from league_skin_manager.settings import (
    DEFAULT_OPACITY,
    DEFAULT_SCALE,
    OPACITY_CHOICES,
    SCALE_CHOICES,
    SCHEMA_VERSION,
    Settings,
    load,
    save,
)


def test_a_missing_file_yields_defaults(tmp_path: Path) -> None:
    result = load(tmp_path / "absent.json")
    assert result == Settings()
    assert result.commit is None
    assert result.skins == 0
    assert result.cooldown_auto_run is False
    assert result.ltk_installed_by_app is False


def test_settings_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    original = Settings(
        commit="a20cc5c71166557a26cc5a3446287be1b99650a5",
        patch="16.15.1",
        skins=1922,
        synced_at="2026-08-19T08:00:00Z",
        ltk_installed_by_app=True,
        cooldown_auto_run=True,
        cooldown_opacity=0.70,
        cooldown_scale=1.25,
        cooldown_left=120,
        cooldown_top=340,
    )
    save(target, original)
    assert load(target) == original


def test_saved_payload_records_the_schema_version(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    save(target, Settings())
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION


def test_cooldown_defaults_are_offered_presets() -> None:
    assert DEFAULT_OPACITY in OPACITY_CHOICES
    assert DEFAULT_SCALE in SCALE_CHOICES


# --- tolerance: this is our own reconstructible state ---------------------


def test_corrupt_json_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{not json at all", encoding="utf-8")
    assert load(target) == Settings()


def test_a_json_array_falls_back_to_defaults(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    assert load(target) == Settings()


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"commit": "abc", "from_a_future_version": {"nested": True}}),
        encoding="utf-8",
    )
    assert load(target).commit == "abc"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("commit", 42),
        ("commit", ""),
        ("patch", []),
        ("skins", -1),
        ("skins", "many"),
        ("skins", True),
        ("ltk_installed_by_app", "yes"),
        ("cooldown_auto_run", 1),
        ("cooldown_left", "120"),
        ("cooldown_top", 3.5),
    ],
)
def test_a_bad_value_falls_back_to_its_default(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({field: bad_value}), encoding="utf-8")
    assert getattr(load(target), field) == getattr(Settings(), field)


# --- display presets ------------------------------------------------------


@pytest.mark.parametrize("value", OPACITY_CHOICES)
def test_every_opacity_preset_survives_a_round_trip(tmp_path: Path, value: float) -> None:
    target = tmp_path / "settings.json"
    save(target, Settings(cooldown_opacity=value))
    assert load(target).cooldown_opacity == value


@pytest.mark.parametrize("value", SCALE_CHOICES)
def test_every_scale_preset_survives_a_round_trip(tmp_path: Path, value: float) -> None:
    target = tmp_path / "settings.json"
    save(target, Settings(cooldown_scale=value))
    assert load(target).cooldown_scale == value


@pytest.mark.parametrize(
    "written,expected",
    [(0.99, 1.0), (0.8, 0.85), (0.6, 0.55), (0.0, 0.55), (5.0, 1.0)],
)
def test_an_off_preset_opacity_snaps_to_the_nearest(
    tmp_path: Path, written: float, expected: float
) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"cooldown_opacity": written}), encoding="utf-8")
    assert load(target).cooldown_opacity == expected


def test_with_display_validates_and_leaves_others_alone() -> None:
    original = Settings(commit="abc", cooldown_scale=1.25)
    updated = original.with_display(opacity=0.71)
    assert updated.cooldown_opacity == 0.70
    assert updated.cooldown_scale == 1.25
    assert updated.commit == "abc"


def test_with_display_ignores_omitted_values() -> None:
    original = Settings(cooldown_opacity=0.55, cooldown_scale=0.70)
    assert original.with_display() == original


def test_with_sync_records_a_completed_sync() -> None:
    updated = Settings().with_sync(
        commit="a20cc5c", patch="16.15.1", skins=1922, synced_at="2026-08-19T08:00:00Z"
    )
    assert updated.commit == "a20cc5c"
    assert updated.patch == "16.15.1"
    assert updated.skins == 1922
    assert updated.synced_at == "2026-08-19T08:00:00Z"


def test_with_sync_preserves_unrelated_settings() -> None:
    original = Settings(ltk_installed_by_app=True, cooldown_auto_run=True, cooldown_scale=0.70)
    updated = original.with_sync(commit="x", patch=None, skins=1, synced_at="t")
    assert updated.ltk_installed_by_app is True
    assert updated.cooldown_auto_run is True
    assert updated.cooldown_scale == 0.70


def test_settings_are_immutable() -> None:
    with pytest.raises((AttributeError, TypeError)):
        Settings().commit = "mutated"  # type: ignore[misc]
