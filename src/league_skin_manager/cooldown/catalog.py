"""Patch-scoped cooldown metadata from Data Dragon.

The live client says who the enemies are; Data Dragon says how long their
abilities take.  Neither exposes live enemy cooldown state, so the board stays
click-to-start -- but nothing has to be typed by hand.

Everything here is validated before use and cached per realm version.  The
rank rules are the subtle part, and they were established empirically rather
than assumed: ultimates are *not* uniformly three ranks, so a naive 6/11/16
mapping is wrong for a real set of champions, and charge-based abilities have
no meaningful flat cooldown at all.  Anything that cannot be inferred from
champion level is marked unsupported and rendered as disabled rather than
guessed at.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from ..atomic import atomic_write_json, read_json
from .roster import RosterMember, SummonerSpellRef
from .timer import CooldownDefinition, EnemyCooldownLoadout

LOGGER = logging.getLogger(__name__)

VERSIONS_URL = "https://ddragon.leagueoflegends.com/api/versions.json"
CDN_ROOT = "https://ddragon.leagueoflegends.com/cdn"

_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

MAX_PAYLOAD_BYTES = 16 * 1024 * 1024
CACHE_TTL_SECONDS = 24 * 60 * 60

# Ultimates whose rank count is not three but which still map onto levels.
ALLOWED_FOUR_RANK_ULTIMATES = frozenset({"elise", "karma", "nidalee"})

# Charge, resource, toggle, or repeat-cast ultimates. A flat base cooldown
# would be actively misleading for these, so they render as unsupported.
DYNAMIC_ULTIMATE_CHAMPIONS = frozenset(
    {
        "anivia",
        "belveth",
        "corki",
        "kassadin",
        "kogmaw",
        "quinn",
        "samira",
        "shyvana",
        "teemo",
        "udyr",
    }
)


class CatalogUnavailable(RuntimeError):
    """Static cooldown metadata could not be obtained."""


@dataclass(frozen=True, slots=True)
class Catalog:
    """One patch's champion and summoner-spell metadata."""

    version: str
    champions: Mapping[str, Mapping[str, Any]]
    summoners: Mapping[str, Mapping[str, Any]]


class CooldownCatalog:
    """Fetches, validates, and caches Data Dragon metadata."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        session: Any | None = None,
        timeout: float = 10.0,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout
        self.logger = logger
        self._session = session
        self._owns_session = session is None
        self._catalog: Catalog | None = None

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                self.logger.debug("Closing the Data Dragon session failed", exc_info=True)
            self._session = None

    # -- public ----------------------------------------------------------

    def loadouts(self, members: Iterable[RosterMember]) -> tuple[EnemyCooldownLoadout, ...]:
        """Resolve each enemy into ultimate and summoner-spell definitions."""

        try:
            catalog = self.ensure_loaded()
        except CatalogUnavailable as error:
            self.logger.info("Cooldown metadata unavailable: %s", error)
            catalog = None
        return tuple(self._loadout(catalog, member) for member in members)

    def ensure_loaded(self) -> Catalog:
        if self._catalog is not None:
            return self._catalog
        version = self._resolve_version()
        champions = self._fetch("championFull.json", version)
        summoners = self._fetch("summoner.json", version)
        self._catalog = Catalog(
            version=version,
            champions=_index(champions, key="id"),
            summoners=_index(summoners, key="id"),
        )
        self.logger.info(
            "Loaded cooldown metadata for patch %s: %d champions, %d summoner spells",
            version,
            len(self._catalog.champions),
            len(self._catalog.summoners),
        )
        return self._catalog

    # -- network with an on-disk fallback ---------------------------------

    def _resolve_version(self) -> str:
        cached = self._read_cache("versions.json")
        if isinstance(cached, str) and _VERSION.fullmatch(cached):
            return cached
        payload = self._get(VERSIONS_URL)
        if not isinstance(payload, list) or not payload:
            raise CatalogUnavailable("Data Dragon returned no versions")
        latest = payload[0]
        if not isinstance(latest, str) or not _VERSION.fullmatch(latest):
            raise CatalogUnavailable(f"Data Dragon returned an unusable version: {latest!r}")
        self._write_cache("versions.json", latest)
        return latest

    def _fetch(self, name: str, version: str) -> Mapping[str, Any]:
        key = f"{version}-{name}"
        cached = self._read_cache(key, ignore_ttl=True)
        if isinstance(cached, Mapping):
            return cached
        payload = self._get(f"{CDN_ROOT}/{version}/data/en_US/{name}")
        if not isinstance(payload, Mapping):
            raise CatalogUnavailable(f"Data Dragon returned an invalid {name}")
        self._write_cache(key, payload)
        return payload

    def _get(self, url: str) -> Any:
        if self._session is None:
            self._session = requests.Session()
        try:
            response = self._session.get(url, timeout=self.timeout)
            response.raise_for_status()
            content = response.content
            if len(content) > MAX_PAYLOAD_BYTES:
                raise CatalogUnavailable(f"Data Dragon payload is implausibly large: {url}")
            return json.loads(content.decode("utf-8"))
        except CatalogUnavailable:
            raise
        except Exception as error:
            raise CatalogUnavailable(f"Could not read {url}") from error

    def _cache_path(self, key: str) -> Path:
        safe = _NON_ALNUM.sub("-", key.casefold()).strip("-")
        return self.cache_dir / f"{safe}.json"

    def _read_cache(self, key: str, *, ignore_ttl: bool = False) -> Any:
        """Read cached data. Version-pinned payloads never expire; the version does."""

        path = self._cache_path(key)
        raw = read_json(path, default=None)
        if not isinstance(raw, Mapping) or "value" not in raw:
            return None
        if not ignore_ttl:
            stamped = raw.get("cached_at")
            if not isinstance(stamped, (int, float)) or isinstance(stamped, bool):
                return None
            if not 0 <= time.time() - float(stamped) < CACHE_TTL_SECONDS:
                return None
        return raw["value"]

    def _write_cache(self, key: str, value: Any) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            atomic_write_json(self._cache_path(key), {"cached_at": time.time(), "value": value})
        except OSError:
            self.logger.debug("Could not cache %s", key, exc_info=True)

    # -- resolution ------------------------------------------------------

    def _loadout(self, catalog: Catalog | None, member: RosterMember) -> EnemyCooldownLoadout:
        ultimate = self._ultimate(catalog, member)
        spells = list(member.summoner_spells) + [None, None]
        return EnemyCooldownLoadout(
            participant_id=member.participant_id or member.champion_name.casefold(),
            champion_name=member.champion_name,
            champion_icon_path=None,
            ultimate=ultimate,
            summoner_spells=(
                self._summoner(catalog, spells[0], 1),
                self._summoner(catalog, spells[1], 2),
            ),
        )

    def _ultimate(self, catalog: Catalog | None, member: RosterMember) -> CooldownDefinition:
        identifier = member.champion_id or member.champion_name
        record = catalog.champions.get(_normalize(identifier)) if catalog else None
        if record is None:
            return _unsupported(
                f"{identifier}R", "Ultimate (R)", "Champion metadata is unavailable"
            )

        spells = record.get("spells")
        payload = spells[3] if isinstance(spells, (list, tuple)) and len(spells) > 3 else None
        if not isinstance(payload, Mapping):
            return _unsupported(
                f"{identifier}R", "Ultimate (R)", "Ultimate metadata is unavailable"
            )

        name = _text(payload.get("name")) or "Ultimate (R)"
        max_rank = _rank(payload.get("maxrank"))
        cooldowns = _cooldowns(payload.get("cooldown"))
        champion = _normalize(identifier)

        reason: str | None = None
        inferable = (
            max_rank == 3
            or (max_rank == 1 and champion == "jayce")
            or (max_rank == 4 and champion in ALLOWED_FOUR_RANK_ULTIMATES)
        )
        if not inferable:
            reason = "Ultimate rank cannot be inferred from champion level"
        if champion in DYNAMIC_ULTIMATE_CHAMPIONS:
            reason = "Ultimate uses charge, resource, toggle, or repeat-cast behaviour"
        if cooldowns is None or len(cooldowns) != max_rank:
            cooldowns = ()
            reason = "Base ultimate cooldown is dynamic or unavailable"

        return CooldownDefinition(
            identifier=_text(payload.get("id")) or f"{identifier}R",
            display_name=name,
            icon_path=None,
            cooldowns=cooldowns or (),
            max_rank=max_rank,
            unsupported_reason=reason,
        )

    def _summoner(
        self, catalog: Catalog | None, reference: SummonerSpellRef | None, slot: int
    ) -> CooldownDefinition:
        fallback_name = (reference.display_name if reference else "") or f"Summoner spell {slot}"
        fallback_id = (reference.identifier if reference else None) or f"unknown-summoner-{slot}"
        record = self._find_summoner(catalog, reference)
        if record is None:
            return _unsupported(
                fallback_id, fallback_name, "Summoner spell metadata is unavailable"
            )

        name = _text(record.get("name")) or fallback_name
        max_rank = _rank(record.get("maxrank"))
        cooldowns = _cooldowns(record.get("cooldown"))
        identifier = _text(record.get("id")) or fallback_id

        reason: str | None = None
        if "smite" in _normalize(identifier) or _normalize(name) == "smite":
            # Smite recharges on a charge system; a flat number would be wrong.
            reason = "Smite uses charge-based cooldown behaviour"
        elif max_rank != 1:
            reason = "Summoner spell rank metadata is unsupported"
        if cooldowns is None or len(cooldowns) != max_rank:
            cooldowns = ()
            reason = "Base summoner spell cooldown is dynamic or unavailable"

        return CooldownDefinition(
            identifier=identifier,
            display_name=name,
            icon_path=None,
            cooldowns=cooldowns or (),
            max_rank=max_rank,
            unsupported_reason=reason,
        )

    @staticmethod
    def _find_summoner(
        catalog: Catalog | None, reference: SummonerSpellRef | None
    ) -> Mapping[str, Any] | None:
        if catalog is None or reference is None:
            return None
        if reference.identifier:
            found = catalog.summoners.get(_normalize(reference.identifier))
            if found is not None:
                return found
        wanted = _normalize(reference.display_name)
        for record in catalog.summoners.values():
            if _normalize(str(record.get("name", ""))) == wanted:
                return record
        return None


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------


def _index(payload: Mapping[str, Any], *, key: str) -> dict[str, Mapping[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise CatalogUnavailable("Data Dragon payload has no data object")
    result: dict[str, Mapping[str, Any]] = {}
    for raw_key, record in data.items():
        if not isinstance(record, Mapping):
            continue
        identifier = _text(record.get(key)) or _text(raw_key)
        if identifier:
            result[_normalize(identifier)] = record
    if not result:
        raise CatalogUnavailable("Data Dragon payload contained no usable records")
    return result


def _cooldowns(value: object) -> tuple[float, ...] | None:
    """Accept only a list of finite positive numbers."""

    if not isinstance(value, (list, tuple)) or not value:
        return None
    result: list[float] = []
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None
        cooldown = float(raw)
        if not math.isfinite(cooldown) or cooldown <= 0.0:
            return None
        result.append(cooldown)
    return tuple(result)


def _rank(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _normalize(value: str) -> str:
    return _NON_ALNUM.sub("", value.casefold())


def _unsupported(identifier: str, name: str, reason: str) -> CooldownDefinition:
    return CooldownDefinition(
        identifier=identifier,
        display_name=name,
        icon_path=None,
        cooldowns=(),
        max_rank=0,
        unsupported_reason=reason,
    )


__all__ = [
    "ALLOWED_FOUR_RANK_ULTIMATES",
    "CDN_ROOT",
    "DYNAMIC_ULTIMATE_CHAMPIONS",
    "VERSIONS_URL",
    "Catalog",
    "CatalogUnavailable",
    "CooldownCatalog",
]
