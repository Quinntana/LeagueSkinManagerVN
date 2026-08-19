"""Immutable roster values shared by the live client and the catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Role(str, Enum):
    TOP = "TOP"
    JUNGLE = "JUNGLE"
    MIDDLE = "MIDDLE"
    BOTTOM = "BOTTOM"
    UTILITY = "UTILITY"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def from_api(cls, value: object) -> Role:
        aliases = {
            "TOP": cls.TOP,
            "JUNGLE": cls.JUNGLE,
            "MIDDLE": cls.MIDDLE,
            "MID": cls.MIDDLE,
            "BOTTOM": cls.BOTTOM,
            "BOT": cls.BOTTOM,
            "UTILITY": cls.UTILITY,
            "SUPPORT": cls.UTILITY,
        }
        return aliases.get(str(value or "").upper(), cls.UNKNOWN)


class RosterStatus(str, Enum):
    ACTIVE = "active"
    UNAVAILABLE = "unavailable"
    INVALID_RESPONSE = "invalid_response"


@dataclass(frozen=True, slots=True)
class SummonerSpellRef:
    identifier: str | None
    display_name: str


@dataclass(frozen=True, slots=True)
class RosterMember:
    """One enemy, as the live client describes them."""

    champion_name: str
    role: Role = Role.UNKNOWN
    participant_id: str = ""
    champion_id: str | None = None
    level: int | None = None
    summoner_spells: tuple[SummonerSpellRef, ...] = ()


@dataclass(frozen=True, slots=True)
class RosterResult:
    status: RosterStatus
    members: tuple[RosterMember, ...] = ()
    error: str | None = None

    @property
    def is_active(self) -> bool:
        return self.status is RosterStatus.ACTIVE


__all__ = ["Role", "RosterMember", "RosterResult", "RosterStatus", "SummonerSpellRef"]
