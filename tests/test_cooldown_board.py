"""Tests for the cooldown board model.

This is the join between the live roster, the Data Dragon catalog, and the
timer store — the piece that removes typing durations by hand.
"""

from __future__ import annotations

from typing import Any

import pytest

from league_skin_manager.cooldown.board import MAX_ROWS, CooldownBoard
from league_skin_manager.cooldown.roster import (
    RosterMember,
    RosterResult,
    RosterStatus,
    SummonerSpellRef,
)
from league_skin_manager.cooldown.timer import (
    CooldownDefinition,
    CooldownSlot,
    CooldownTimerStore,
    EnemyCooldownLoadout,
)


class FakeClock:
    """The Clock protocol the timer store needs: monotonic plus wall time."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def timestamp(self) -> float:
        return 1_760_000_000.0 + self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def definition(
    name: str, cooldowns: tuple[float, ...] = (120.0, 100.0, 80.0)
) -> CooldownDefinition:
    return CooldownDefinition(
        identifier=name,
        display_name=name,
        icon_path=None,
        cooldowns=cooldowns,
        max_rank=len(cooldowns),
        unsupported_reason=None,
    )


def unsupported(name: str, reason: str = "charge based") -> CooldownDefinition:
    return CooldownDefinition(
        identifier=name,
        display_name=name,
        icon_path=None,
        cooldowns=(),
        max_rank=0,
        unsupported_reason=reason,
    )


def member(name: str, *, level: int | None = 11) -> RosterMember:
    return RosterMember(
        champion_name=name,
        participant_id=f"participant-{name}",
        champion_id=name,
        level=level,
        summoner_spells=(SummonerSpellRef("SummonerFlash", "Flash"),),
    )


def loadout(name: str, *, ultimate: CooldownDefinition | None = None) -> EnemyCooldownLoadout:
    return EnemyCooldownLoadout(
        participant_id=f"participant-{name}",
        champion_name=name,
        champion_icon_path=None,
        ultimate=ultimate or definition(f"{name}R"),
        summoner_spells=(definition("Flash", (300.0,)), unsupported("Smite")),
    )


def make_board(
    members: list[RosterMember] | None = None,
    *,
    status: RosterStatus = RosterStatus.ACTIVE,
    clock: FakeClock | None = None,
    resolver: Any = None,
) -> tuple[CooldownBoard, FakeClock, dict[str, int]]:
    clock = clock or FakeClock()
    calls = {"roster": 0, "resolve": 0}
    people = members if members is not None else [member("Zed"), member("Lux")]

    def roster() -> RosterResult:
        calls["roster"] += 1
        return RosterResult(status, tuple(people))

    def resolve(people_in: Any) -> tuple[EnemyCooldownLoadout, ...]:
        calls["resolve"] += 1
        return tuple(loadout(m.champion_name) for m in people_in)

    board = CooldownBoard(
        CooldownTimerStore(clock, None),
        roster=roster,
        resolve=resolver or resolve,
    )
    return board, clock, calls


# --- roster ---------------------------------------------------------------


def test_an_active_match_is_reported() -> None:
    board, _clock, _calls = make_board()
    assert board.refresh() is True


def test_no_match_is_reported_without_raising() -> None:
    board, _clock, calls = make_board(status=RosterStatus.UNAVAILABLE)
    assert board.refresh() is False
    assert calls["resolve"] == 0, "no catalog work outside a match"


def test_the_catalog_is_consulted_once_per_match() -> None:
    board, _clock, calls = make_board()
    board.refresh()
    board.refresh()
    board.refresh()
    assert calls["roster"] == 3
    assert calls["resolve"] == 1, "identities do not change within a match"


def test_a_new_roster_is_treated_as_a_new_match() -> None:
    clock = FakeClock()
    people = [member("Zed")]
    board, _clock, calls = make_board(people, clock=clock)
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    assert board.rows()[0].slots[0].counting

    people[:] = [member("Ahri")]
    board.refresh()

    assert calls["resolve"] == 2
    assert not board.rows()[0].slots[0].counting, "timers must not survive into a new match"


def test_a_failing_roster_source_is_survived() -> None:
    def explode() -> RosterResult:
        raise OSError("client gone")

    board = CooldownBoard(
        CooldownTimerStore(FakeClock(), None), roster=explode, resolve=lambda _m: ()
    )
    assert board.refresh() is False


def test_a_failing_catalog_leaves_the_board_usable() -> None:
    def explode(_members: Any) -> tuple[EnemyCooldownLoadout, ...]:
        raise OSError("data dragon down")

    board, _clock, _calls = make_board(resolver=explode)
    assert board.refresh() is True
    assert len(board.rows()) == MAX_ROWS


# --- rendering ------------------------------------------------------------


def test_the_board_always_has_five_rows() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    rows = board.rows()
    assert len(rows) == MAX_ROWS
    assert rows[0].champion == "Zed"
    assert rows[1].is_placeholder


def test_champion_and_level_come_from_the_live_client() -> None:
    board, _clock, _calls = make_board([member("Zed", level=16)])
    board.refresh()
    row = board.rows()[0]
    assert row.champion == "Zed"
    assert row.level == 16


def test_a_startable_slot_shows_its_letter() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    assert board.rows()[0].slots[0].caption == "R"
    assert board.rows()[0].slots[0].enabled is True


def test_an_unsupported_slot_is_disabled_with_a_reason() -> None:
    """Smite is charge-based; showing a number would be wrong."""

    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    smite = board.rows()[0].slots[2]
    assert smite.enabled is False
    assert smite.caption == "-"
    assert smite.reason is not None


def test_a_slot_not_yet_learned_is_disabled() -> None:
    board, _clock, _calls = make_board([member("Zed", level=3)])
    board.refresh()
    ultimate = board.rows()[0].slots[0]
    assert ultimate.enabled is False
    assert "level 3" in (ultimate.reason or "")


# --- the click cycle ------------------------------------------------------


def test_a_click_starts_a_timer_with_the_real_duration() -> None:
    board, _clock, _calls = make_board([member("Zed", level=11)])
    board.refresh()
    snapshot = board.press(0, CooldownSlot.ULTIMATE)
    assert snapshot is not None
    assert snapshot.remaining == pytest.approx(100.0), "rank 2 at level 11"


def test_a_second_click_cancels() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    assert board.press(0, CooldownSlot.ULTIMATE) is None
    assert board.rows()[0].slots[0].counting is False


def test_a_third_click_starts_fresh_rather_than_resuming() -> None:
    board, clock, _calls = make_board([member("Zed")])
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    clock.advance(60)
    board.press(0, CooldownSlot.ULTIMATE)  # cancel
    restarted = board.press(0, CooldownSlot.ULTIMATE)
    assert restarted is not None
    assert restarted.remaining == pytest.approx(100.0), "a restart is not a resume"


def test_the_countdown_decreases() -> None:
    board, clock, _calls = make_board([member("Zed")])
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    clock.advance(40)
    assert board.rows()[0].slots[0].remaining == pytest.approx(60.0)


def test_a_finished_timer_reads_ready() -> None:
    board, clock, _calls = make_board([member("Zed")])
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    clock.advance(500)
    slot = board.rows()[0].slots[0]
    assert slot.is_ready is True
    assert slot.caption == "up"


def test_clicking_an_unsupported_slot_does_nothing() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    assert board.press(0, CooldownSlot.SPELL_TWO) is None


def test_clicking_an_empty_row_does_nothing() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    assert board.press(4, CooldownSlot.ULTIMATE) is None


def test_clearing_removes_every_timer() -> None:
    board, _clock, _calls = make_board()
    board.refresh()
    board.press(0, CooldownSlot.ULTIMATE)
    board.press(1, CooldownSlot.ULTIMATE)
    assert board.clear_all() == 2
    assert not any(slot.counting for row in board.rows() for slot in row.slots)


def test_summoner_spells_use_their_own_duration() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    snapshot = board.press(0, CooldownSlot.SPELL_ONE)
    assert snapshot is not None
    assert snapshot.remaining == pytest.approx(300.0), "Flash, from Data Dragon"
