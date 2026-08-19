"""Tests for the Live Client Data adapter."""

from __future__ import annotations

from typing import Any

import pytest
import requests

from league_skin_manager.cooldown.live import LiveClient
from league_skin_manager.cooldown.roster import Role, RosterStatus


def player(
    name: str,
    champion: str,
    team: str,
    *,
    level: int | None = 11,
    position: str = "MIDDLE",
    spells: dict[str, Any] | None = None,
    raw: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "riotId": name,
        "championName": champion,
        "team": team,
        "position": position,
    }
    if level is not None:
        record["level"] = level
    if raw is not None:
        record["rawChampionName"] = raw
    if spells is not None:
        record["summonerSpells"] = spells
    return record


def spell(display: str, identifier: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"displayName": display}
    if identifier:
        entry["rawDisplayName"] = f"GeneratedTip_SummonerSpell_{identifier}_DisplayName"
    return entry


class FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Any:
        self.calls.append(url)
        if isinstance(self._payload, Exception):
            raise self._payload
        return FakeResponse(self._payload)

    def close(self) -> None:
        return None


def client_for(payload: Any) -> LiveClient:
    return LiveClient(session=FakeSession(payload))


GAME = {
    "activePlayer": {"riotId": "Me#EUW"},
    "allPlayers": [
        player("Me#EUW", "Ahri", "ORDER"),
        player("Ally#EUW", "Thresh", "ORDER"),
        player(
            "Foe#EUW",
            "Zed",
            "CHAOS",
            level=13,
            raw="game_character_displayname_Zed",
            spells={
                "summonerSpellOne": spell("Flash", "SummonerFlash"),
                "summonerSpellTwo": spell("Ignite", "SummonerDot"),
            },
        ),
        player("Foe2#EUW", "Lux", "CHAOS", level=6, position="UTILITY"),
    ],
}


def test_only_the_enemy_team_is_returned() -> None:
    result = client_for(GAME).enemy_roster()
    assert result.status is RosterStatus.ACTIVE
    assert {member.champion_name for member in result.members} == {"Zed", "Lux"}


def test_levels_and_roles_are_parsed() -> None:
    members = {m.champion_name: m for m in client_for(GAME).enemy_roster().members}
    assert members["Zed"].level == 13
    assert members["Lux"].role is Role.UTILITY


def test_summoner_spells_are_parsed_with_identifiers() -> None:
    members = {m.champion_name: m for m in client_for(GAME).enemy_roster().members}
    spells = members["Zed"].summoner_spells
    assert [s.display_name for s in spells] == ["Flash", "Ignite"]
    assert [s.identifier for s in spells] == ["SummonerFlash", "SummonerDot"]


def test_the_champion_id_is_extracted_from_the_raw_name() -> None:
    members = {m.champion_name: m for m in client_for(GAME).enemy_roster().members}
    assert members["Zed"].champion_id == "Zed"


def test_participant_ids_are_hashed_not_stored() -> None:
    """Riot IDs must not survive into memory or logs."""

    members = client_for(GAME).enemy_roster().members
    for member in members:
        assert member.participant_id.startswith("participant-")
        assert "Foe" not in member.participant_id
        assert "#" not in member.participant_id


def test_participant_ids_are_stable_and_distinct() -> None:
    first = client_for(GAME).enemy_roster().members
    second = client_for(GAME).enemy_roster().members
    assert [m.participant_id for m in first] == [m.participant_id for m in second]
    assert len({m.participant_id for m in first}) == len(first)


def test_no_match_is_unavailable_not_an_error() -> None:
    """Outside a game the endpoint simply is not there; that is normal."""

    result = client_for(requests.ConnectionError("refused")).enemy_roster()
    assert result.status is RosterStatus.UNAVAILABLE
    assert result.members == ()


def test_an_ambiguous_active_player_is_rejected() -> None:
    payload = {
        "activePlayer": {"riotId": "Me#EUW"},
        "allPlayers": [player("Me#EUW", "Ahri", "ORDER"), player("Me#EUW", "Zed", "CHAOS")],
    }
    assert client_for(payload).enemy_roster().status is RosterStatus.INVALID_RESPONSE


def test_a_missing_active_identity_is_rejected() -> None:
    payload = {"activePlayer": {}, "allPlayers": []}
    assert client_for(payload).enemy_roster().status is RosterStatus.INVALID_RESPONSE


def test_an_oversized_player_list_is_rejected() -> None:
    payload = {
        "activePlayer": {"riotId": "Me#EUW"},
        "allPlayers": [player(f"P{i}#EUW", "Ahri", "ORDER") for i in range(100)],
    }
    assert client_for(payload).enemy_roster().status is RosterStatus.INVALID_RESPONSE


@pytest.mark.parametrize("level", [0, 19, -1, True, "11", None])
def test_an_implausible_level_becomes_none(level: Any) -> None:
    payload = {
        "activePlayer": {"riotId": "Me#EUW"},
        "allPlayers": [
            player("Me#EUW", "Ahri", "ORDER"),
            {"riotId": "F#EUW", "championName": "Zed", "team": "CHAOS", "level": level},
        ],
    }
    members = client_for(payload).enemy_roster().members
    assert members[0].level is None


def test_a_non_object_payload_is_rejected() -> None:
    assert client_for(["not", "an", "object"]).enemy_roster().status is RosterStatus.INVALID_RESPONSE


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True])
def test_an_invalid_timeout_is_rejected(timeout: Any) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        LiveClient(timeout=timeout)


def test_the_timeout_is_capped() -> None:
    assert LiveClient(timeout=999.0).timeout == 5.0
