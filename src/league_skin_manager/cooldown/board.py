"""The UI-free cooldown board model.

This is what replaces typing durations by hand.  The live client supplies the
enemy roster, Data Dragon supplies the base cooldowns, and the board joins
them into rows a window can render without knowing about either source.

Neither source exposes enemy cast events, so a click still starts the timer.
What the click no longer needs is a number.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Lock

from .roster import RosterMember, RosterResult, RosterStatus
from .timer import (
    CooldownDefinition,
    CooldownKey,
    CooldownSlot,
    CooldownSnapshot,
    CooldownTimerStore,
    EnemyCooldownLoadout,
)

LOGGER = logging.getLogger(__name__)

MAX_ROWS = 5
SLOTS = (CooldownSlot.ULTIMATE, CooldownSlot.SPELL_ONE, CooldownSlot.SPELL_TWO)
SLOT_LABELS = {CooldownSlot.ULTIMATE: "R", CooldownSlot.SPELL_ONE: "D", CooldownSlot.SPELL_TWO: "F"}

RosterProvider = Callable[[], RosterResult]
LoadoutResolver = Callable[[Iterable[RosterMember]], tuple[EnemyCooldownLoadout, ...]]


@dataclass(frozen=True, slots=True)
class SlotView:
    """One clickable square, already resolved into what to draw."""

    slot: CooldownSlot
    label: str
    caption: str
    remaining: float | None
    is_ready: bool
    enabled: bool
    reason: str | None

    @property
    def counting(self) -> bool:
        return self.remaining is not None and not self.is_ready


@dataclass(frozen=True, slots=True)
class RowView:
    """One enemy."""

    champion: str
    level: int | None
    slots: tuple[SlotView, ...]

    @property
    def is_placeholder(self) -> bool:
        return not self.champion


class CooldownBoard:
    """Joins roster, catalog, and timers into renderable rows."""

    def __init__(
        self,
        store: CooldownTimerStore,
        *,
        roster: RosterProvider,
        resolve: LoadoutResolver,
        logger: logging.Logger = LOGGER,
    ) -> None:
        self._store = store
        self._roster = roster
        self._resolve = resolve
        self._logger = logger
        self._lock = Lock()
        self._loadouts: tuple[EnemyCooldownLoadout, ...] = ()
        self._levels: dict[str, int | None] = {}
        self._session: str | None = None

    # -- roster ----------------------------------------------------------

    def refresh(self) -> bool:
        """Poll the live client. Returns whether a match is currently active.

        A roster that changes identity is treated as a new match and resets
        every timer: carrying a previous game's countdowns into a new one
        would be worse than showing nothing.
        """

        try:
            result = self._roster()
        except Exception:  # noqa: BLE001 - the board must survive any source
            self._logger.debug("Roster poll failed", exc_info=True)
            return False

        if result.status is not RosterStatus.ACTIVE:
            if result.status is RosterStatus.INVALID_RESPONSE:
                self._logger.info("Live client returned an unusable roster: %s", result.error)
            return False

        session = "|".join(sorted(member.participant_id for member in result.members))
        with self._lock:
            changed = session != self._session
            self._levels = {m.participant_id: m.level for m in result.members}
        if changed:
            self._store.reset_session(session)
            try:
                loadouts = self._resolve(result.members)
            except Exception:  # noqa: BLE001 - a catalog failure must not break the board
                self._logger.warning("Could not resolve cooldown metadata", exc_info=True)
                loadouts = ()
            with self._lock:
                self._session = session
                self._loadouts = loadouts
            self._logger.info("Cooldown board tracking %d enemies", len(loadouts))
        return True

    # -- rendering -------------------------------------------------------

    def rows(self) -> tuple[RowView, ...]:
        with self._lock:
            loadouts = self._loadouts
            levels = dict(self._levels)

        views: list[RowView] = []
        for loadout in loadouts[:MAX_ROWS]:
            level = levels.get(loadout.participant_id)
            views.append(
                RowView(
                    champion=loadout.champion_name,
                    level=level,
                    slots=tuple(self._slot_view(loadout, slot, level) for slot in SLOTS),
                )
            )
        while len(views) < MAX_ROWS:
            views.append(RowView(champion="", level=None, slots=tuple(_empty_slots())))
        return tuple(views)

    def _slot_view(
        self, loadout: EnemyCooldownLoadout, slot: CooldownSlot, level: int | None
    ) -> SlotView:
        definition = loadout.definition_for(slot)
        snapshot = self._store.snapshot(CooldownKey(loadout.participant_id, slot))
        duration = definition.duration_for_level(level) if level else None
        counting = snapshot is not None and not snapshot.is_ready

        if snapshot is not None and counting:
            caption = f"{int(snapshot.remaining) + 1}"
        elif snapshot is not None:
            caption = "up"
        elif duration is not None:
            caption = SLOT_LABELS[slot]
        else:
            caption = "-"

        return SlotView(
            slot=slot,
            label=SLOT_LABELS[slot],
            caption=caption,
            remaining=None if snapshot is None else snapshot.remaining,
            is_ready=bool(snapshot is not None and snapshot.is_ready),
            # Clickable when it can be started, or when it is running and can
            # therefore be cancelled.
            enabled=duration is not None or counting,
            reason=definition.unsupported_reason or _why_not(definition, level),
        )

    # -- interaction -----------------------------------------------------

    def press(self, row: int, slot: CooldownSlot) -> CooldownSnapshot | None:
        """Left click: start, or cancel a running timer.

        The cycle is idle -> counting -> idle -> counting, and a restart is a
        fresh timer rather than a resumed one.
        """

        with self._lock:
            loadouts = self._loadouts
            levels = dict(self._levels)
        if row >= len(loadouts):
            return None
        loadout = loadouts[row]
        key = CooldownKey(loadout.participant_id, slot)

        snapshot = self._store.snapshot(key)
        if snapshot is not None and not snapshot.is_ready:
            self._store.clear(key)
            return None

        level = levels.get(loadout.participant_id)
        definition = loadout.definition_for(slot)
        if level is None or definition.duration_for_level(level) is None:
            return None
        return self._store.start(key, loadout.champion_name, definition, level)

    def clear_all(self) -> int:
        return self._store.clear_all()


def _empty_slots() -> list[SlotView]:
    return [
        SlotView(
            slot=slot,
            label=SLOT_LABELS[slot],
            caption="-",
            remaining=None,
            is_ready=False,
            enabled=False,
            reason="Waiting for the enemy roster",
        )
        for slot in SLOTS
    ]


def _why_not(definition: CooldownDefinition, level: int | None) -> str | None:
    if level is None:
        return "Waiting for the enemy level"
    if definition.duration_for_level(level) is None:
        return f"Not learned at level {level}"
    return None


__all__ = [
    "MAX_ROWS",
    "SLOTS",
    "SLOT_LABELS",
    "CooldownBoard",
    "RowView",
    "SlotView",
]
