"""Tests for Data Dragon cooldown metadata.

The rank rules are the subtle part. Ultimates are not uniformly three ranks,
and charge-based abilities have no meaningful flat cooldown, so anything that
cannot be inferred from champion level must render as unsupported rather than
be guessed at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.cooldown.catalog import (
    CatalogUnavailable,
    CooldownCatalog,
)
from league_skin_manager.cooldown.roster import RosterMember, SummonerSpellRef

VERSION = "16.15.1"


def champion(identifier: str, *, maxrank: int = 3, cooldown: list[float] | None = None) -> Any:
    ultimate: dict[str, Any] = {
        "id": f"{identifier}R",
        "name": f"{identifier} Ultimate",
        "maxrank": maxrank,
    }
    if cooldown is not None:
        ultimate["cooldown"] = cooldown
    return {
        "id": identifier,
        "name": identifier,
        "spells": [
            {"id": f"{identifier}Q"},
            {"id": f"{identifier}W"},
            {"id": f"{identifier}E"},
            ultimate,
        ],
    }


def summoner(
    identifier: str, name: str, *, maxrank: int = 1, cooldown: list[float] | None = None
) -> Any:
    record: dict[str, Any] = {"id": identifier, "name": name, "maxrank": maxrank}
    if cooldown is not None:
        record["cooldown"] = cooldown
    return record


CHAMPIONS = {
    "type": "champion",
    "data": {
        "Zed": champion("Zed", maxrank=3, cooldown=[120.0, 100.0, 80.0]),
        "Jayce": champion("Jayce", maxrank=1, cooldown=[6.0]),
        "Karma": champion("Karma", maxrank=4, cooldown=[80.0, 70.0, 60.0, 50.0]),
        "Teemo": champion("Teemo", maxrank=3, cooldown=[35.0, 31.0, 27.0]),
        "Udyr": champion("Udyr", maxrank=3, cooldown=[10.0, 9.0, 8.0]),
        "Ryze": champion("Ryze", maxrank=2, cooldown=[240.0, 180.0]),
        "Nomatch": champion("Nomatch", maxrank=3, cooldown=None),
    },
}

SUMMONERS = {
    "type": "summoner",
    "data": {
        "SummonerFlash": summoner("SummonerFlash", "Flash", cooldown=[300.0]),
        "SummonerDot": summoner("SummonerDot", "Ignite", cooldown=[180.0]),
        "SummonerSmite": summoner("SummonerSmite", "Smite", cooldown=[15.0]),
    },
}


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.calls.append(url)
        if url.endswith("versions.json"):
            return FakeResponse([VERSION, "16.14.1"])
        if url.endswith("championFull.json"):
            return FakeResponse(CHAMPIONS)
        if url.endswith("summoner.json"):
            return FakeResponse(SUMMONERS)
        raise AssertionError(f"unexpected url {url}")

    def close(self) -> None:
        return None


def catalog_for(tmp_path: Path) -> tuple[CooldownCatalog, FakeSession]:
    session = FakeSession()
    return CooldownCatalog(tmp_path, session=session), session


def member(champion_id: str, *, level: int = 11, spells: tuple[Any, ...] = ()) -> RosterMember:
    return RosterMember(
        champion_name=champion_id,
        participant_id=f"participant-{champion_id}",
        champion_id=champion_id,
        level=level,
        summoner_spells=spells,
    )


def only(catalog: CooldownCatalog, target: RosterMember) -> Any:
    return catalog.loadouts([target])[0]


# --- loading --------------------------------------------------------------


def test_the_catalog_loads_and_indexes(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    loaded = catalog.ensure_loaded()
    assert loaded.version == VERSION
    assert "zed" in loaded.champions
    assert "summonerflash" in loaded.summoners


def test_the_catalog_is_loaded_once(tmp_path: Path) -> None:
    catalog, session = catalog_for(tmp_path)
    catalog.ensure_loaded()
    before = len(session.calls)
    catalog.ensure_loaded()
    assert len(session.calls) == before


def test_payloads_are_cached_on_disk(tmp_path: Path) -> None:
    catalog, session = catalog_for(tmp_path)
    catalog.ensure_loaded()
    first = len(session.calls)

    fresh = CooldownCatalog(tmp_path, session=session)
    fresh.ensure_loaded()

    assert len(session.calls) == first, "a second instance should read from disk"


def test_an_offline_first_run_reports_unavailable(tmp_path: Path) -> None:
    class Dead:
        def get(self, _url: str, **_kwargs: Any) -> Any:
            raise OSError("offline")

        def close(self) -> None:
            return None

    catalog = CooldownCatalog(tmp_path, session=Dead())
    with pytest.raises(CatalogUnavailable):
        catalog.ensure_loaded()


def test_loadouts_survive_an_unavailable_catalog(tmp_path: Path) -> None:
    """A Data Dragon outage must not break the board, only disable it."""

    class Dead:
        def get(self, _url: str, **_kwargs: Any) -> Any:
            raise OSError("offline")

        def close(self) -> None:
            return None

    catalog = CooldownCatalog(tmp_path, session=Dead())
    loadout = only(catalog, member("Zed"))
    assert loadout.ultimate.unsupported_reason is not None
    assert loadout.ultimate.duration_for_level(11) is None


# --- ultimate rank rules --------------------------------------------------


def test_a_three_rank_ultimate_maps_to_levels_6_11_16(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Zed")).ultimate
    assert ultimate.unsupported_reason is None
    assert ultimate.duration_for_level(5) is None
    assert ultimate.duration_for_level(6) == 120.0
    assert ultimate.duration_for_level(11) == 100.0
    assert ultimate.duration_for_level(16) == 80.0


def test_jayce_is_the_supported_one_rank_ultimate(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Jayce", level=1)).ultimate
    assert ultimate.unsupported_reason is None
    assert ultimate.duration_for_level(1) == 6.0


def test_a_four_rank_ultimate_is_supported_for_known_champions(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Karma")).ultimate
    assert ultimate.unsupported_reason is None
    assert ultimate.duration_for_level(1) == 80.0
    assert ultimate.duration_for_level(16) == 50.0


def test_a_charge_based_ultimate_is_unsupported(tmp_path: Path) -> None:
    """Teemo's ultimate has charges; a flat number would be misleading."""

    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Teemo")).ultimate
    assert ultimate.unsupported_reason is not None
    assert "charge" in ultimate.unsupported_reason


def test_udyr_is_unsupported(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    assert only(catalog, member("Udyr")).ultimate.unsupported_reason is not None


def test_an_uninferable_rank_count_is_unsupported(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Ryze")).ultimate
    assert ultimate.unsupported_reason is not None
    assert ultimate.duration_for_level(16) is None


def test_missing_cooldown_data_is_unsupported(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("Nomatch")).ultimate
    assert ultimate.cooldowns == ()
    assert ultimate.unsupported_reason is not None


def test_an_unknown_champion_is_unsupported(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    ultimate = only(catalog, member("NotAChampion")).ultimate
    assert ultimate.unsupported_reason is not None


# --- summoner spells ------------------------------------------------------


def test_summoner_spells_resolve_by_identifier(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    target = member("Zed", spells=(SummonerSpellRef("SummonerFlash", "Flash"),))
    first = only(catalog, target).summoner_spells[0]
    assert first.display_name == "Flash"
    assert first.duration_for_level(11) == 300.0


def test_summoner_spells_resolve_by_display_name(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    target = member("Zed", spells=(SummonerSpellRef(None, "Ignite"),))
    first = only(catalog, target).summoner_spells[0]
    assert first.duration_for_level(11) == 180.0


def test_smite_is_unsupported(tmp_path: Path) -> None:
    """Smite recharges on charges, so its static cooldown is not a timer."""

    catalog, _ = catalog_for(tmp_path)
    target = member("Zed", spells=(SummonerSpellRef("SummonerSmite", "Smite"),))
    first = only(catalog, target).summoner_spells[0]
    assert first.unsupported_reason is not None
    assert "charge" in first.unsupported_reason


def test_an_absent_spell_is_unsupported_but_present(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    loadout = only(catalog, member("Zed"))
    assert len(loadout.summoner_spells) == 2
    for definition in loadout.summoner_spells:
        assert definition.unsupported_reason is not None


def test_every_enemy_produces_a_loadout(tmp_path: Path) -> None:
    catalog, _ = catalog_for(tmp_path)
    members = [member("Zed"), member("Karma"), member("Teemo")]
    loadouts = catalog.loadouts(members)
    assert len(loadouts) == 3
    assert [item.champion_name for item in loadouts] == ["Zed", "Karma", "Teemo"]
