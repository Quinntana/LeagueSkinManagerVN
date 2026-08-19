"""Riot's Live Client Data API: who the enemies are, and what they brought.

The API is documented and served locally at 127.0.0.1:2999 during a match. It
identifies participants, their champion, level, and summoner spells.

It does **not** expose enemy cast events or enemy cooldown state, which is why
the board is click-to-start rather than automatic. What this adapter provides
is the identities and durations, so nothing has to be typed by hand.

The endpoint uses a self-signed certificate, so verification is disabled for
this host alone -- it is a loopback address serving the local game client.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Mapping
from typing import Any

import requests

from .roster import Role, RosterMember, RosterResult, RosterStatus, SummonerSpellRef

LOGGER = logging.getLogger(__name__)

BASE_URL = "https://127.0.0.1:2999/liveclientdata"

_RAW_CHAMPION_ID = re.compile(r"(?:^|_)(?P<identifier>[A-Za-z][A-Za-z0-9]*)$")
_RAW_SPELL_ID = re.compile(
    r"^GeneratedTip_SummonerSpell_(?P<identifier>[A-Za-z0-9_]+)_DisplayName$"
)
_SPELL_KEYS = ("summonerSpellOne", "summonerSpellTwo")
_OPPOSING_TEAM = {"ORDER": "CHAOS", "CHAOS": "ORDER"}

MAX_TIMEOUT_SECONDS = 5.0
MAX_PLAYER_RECORDS = 64
MAX_TEXT_LENGTH = 256


class LiveClient:
    """Reads the enemy roster from the local game client."""

    def __init__(
        self,
        *,
        timeout: float = 1.0,
        session: Any | None = None,
        logger: logging.Logger = LOGGER,
    ) -> None:
        if isinstance(timeout, bool) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout = min(timeout, MAX_TIMEOUT_SECONDS)
        self.logger = logger
        self._session = session
        self._owns_session = session is None

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001 - closing must never raise
                self.logger.debug("Closing the live client session failed", exc_info=True)
            self._session = None

    def _get(self, endpoint: str) -> Any:
        if self._session is None:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            self._session = requests.Session()
        response = self._session.get(
            f"{BASE_URL}/{endpoint}",
            timeout=self.timeout,
            verify=False,  # noqa: S501 - loopback, self-signed by the game client
        )
        response.raise_for_status()
        return response.json()

    def enemy_roster(self) -> RosterResult:
        """Return the enemy team, or why it is unavailable.

        Unavailable is the normal state outside a match and is not an error.
        """

        try:
            payload = self._get("allgamedata")
        except Exception as error:  # noqa: BLE001 - any failure means "no match"
            return RosterResult(
                RosterStatus.UNAVAILABLE,
                error=f"Live Client request failed ({type(error).__name__})",
            )
        if not isinstance(payload, Mapping):
            return _invalid("Live Client returned a non-object payload")
        return _parse(payload.get("activePlayer"), payload.get("allPlayers"))


def _parse(active: object, players: object) -> RosterResult:
    tokens = _identity_tokens(active)
    if not tokens:
        return _invalid("Live Client did not return a valid active-player identity")
    if not isinstance(players, list) or len(players) > MAX_PLAYER_RECORDS:
        return _invalid("Live Client returned an invalid player list")

    matching = [
        player
        for player in players
        if isinstance(player, Mapping) and tokens.intersection(_identity_tokens(player))
    ]
    if len(matching) != 1:
        return _invalid("Active player was not uniquely present in the player list")

    own_team = _team(matching[0].get("team"))
    if own_team is None:
        return _invalid("Active player did not have a recognised team")
    enemy_team = _OPPOSING_TEAM[own_team]

    members: list[RosterMember] = []
    for player in players:
        if not isinstance(player, Mapping) or _team(player.get("team")) != enemy_team:
            continue
        champion_name = _text(player.get("championName"))
        if champion_name is None:
            continue
        champion_id = _champion_id(player.get("rawChampionName"))
        members.append(
            RosterMember(
                champion_name=champion_name,
                role=Role.from_api(player.get("position")),
                participant_id=_participant_id(player, champion_name, champion_id),
                champion_id=champion_id,
                level=_level(player.get("level")),
                summoner_spells=_spells(player.get("summonerSpells")),
            )
        )
    return RosterResult(RosterStatus.ACTIVE, tuple(members))


def _invalid(error: str) -> RosterResult:
    return RosterResult(RosterStatus.INVALID_RESPONSE, error=error)


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_TEXT_LENGTH:
        return None
    return cleaned


def _team(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    upper = text.upper()
    return upper if upper in _OPPOSING_TEAM else None


def _level(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 1 <= value <= 18 else None


def _champion_id(value: object) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = _RAW_CHAMPION_ID.search(text)
    return match.group("identifier") if match else None


def _spells(value: object) -> tuple[SummonerSpellRef, ...]:
    if not isinstance(value, Mapping):
        return ()
    refs: list[SummonerSpellRef] = []
    for key in _SPELL_KEYS:
        entry = value.get(key)
        if not isinstance(entry, Mapping):
            continue
        display = _text(entry.get("displayName"))
        if display is None:
            continue
        identifier = None
        raw = _text(entry.get("rawDisplayName"))
        if raw is not None:
            match = _RAW_SPELL_ID.fullmatch(raw)
            if match:
                identifier = match.group("identifier")
        refs.append(SummonerSpellRef(identifier, display))
    return tuple(refs)


def _identity_tokens(value: object) -> frozenset[str]:
    """Collect the identity strings a player record may be matched on."""

    if not isinstance(value, Mapping):
        return frozenset()
    tokens: set[str] = set()
    for key in ("riotId", "summonerName", "riotIdGameName"):
        text = _text(value.get(key))
        if text is not None:
            tokens.add(text.casefold())
    game_name = _text(value.get("riotIdGameName"))
    tag_line = _text(value.get("riotIdTagLine"))
    if game_name is not None and tag_line is not None:
        tokens.add(f"{game_name}#{tag_line}".casefold())
    return frozenset(tokens)


def _participant_id(
    player: Mapping[str, object], champion_name: str, champion_id: str | None
) -> str:
    """Derive a stable, non-identifying key for one participant.

    Riot IDs are hashed rather than stored so nothing player-identifying is
    kept in memory or written to a log.
    """

    identity = _text(player.get("riotId"))
    if identity is None:
        game_name = _text(player.get("riotIdGameName"))
        tag_line = _text(player.get("riotIdTagLine"))
        if game_name is not None and tag_line is not None:
            identity = f"{game_name}#{tag_line}"
    identity = identity or _text(player.get("summonerName"))
    if identity is not None:
        material = f"identity:{identity.casefold()}"
    else:
        team = _team(player.get("team")) or "unknown-team"
        material = f"fallback:{team.casefold()}:{(champion_id or champion_name).casefold()}"
    return f"participant-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:20]}"


__all__ = ["BASE_URL", "MAX_PLAYER_RECORDS", "MAX_TIMEOUT_SECONDS", "LiveClient"]
