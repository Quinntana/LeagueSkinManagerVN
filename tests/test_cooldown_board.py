"""Tests for the cooldown board model.

This is the join between the live roster, the Data Dragon catalog, and the
timer store — the piece that removes typing durations by hand.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.cooldown.board import (
    MAX_ROWS,
    CooldownBoard,
    RosterPoller,
    order_by_lane,
)
from league_skin_manager.cooldown.roster import (
    Role,
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


# --- lane ordering ---------------------------------------------------------


def positioned(name: str, role: Role) -> RosterMember:
    return RosterMember(
        champion_name=name,
        role=role,
        participant_id=f"participant-{name}",
        champion_id=name,
        level=11,
        summoner_spells=(SummonerSpellRef("SummonerFlash", "Flash"),),
    )


def test_rows_are_ordered_by_lane() -> None:
    scrambled = [
        positioned("Lux", Role.MIDDLE),
        positioned("Ornn", Role.TOP),
        positioned("Leona", Role.UTILITY),
        positioned("Jinx", Role.BOTTOM),
        positioned("LeeSin", Role.JUNGLE),
    ]
    ordered = order_by_lane(scrambled)
    assert [m.champion_name for m in ordered] == ["Ornn", "LeeSin", "Lux", "Jinx", "Leona"]


def test_a_mode_without_lanes_keeps_the_live_client_order() -> None:
    """ARAM reports no position at all; a stable sort leaves it untouched."""

    given = [member(name) for name in ("Zed", "Lux", "Ornn", "Jinx", "Leona")]
    ordered = order_by_lane(given)
    assert [m.champion_name for m in ordered] == ["Zed", "Lux", "Ornn", "Jinx", "Leona"]


def test_unpositioned_enemies_sort_after_positioned_ones() -> None:
    given = [member("Zed"), positioned("Ornn", Role.TOP), member("Lux")]
    ordered = order_by_lane(given)
    assert [m.champion_name for m in ordered] == ["Ornn", "Zed", "Lux"]


def test_ordering_is_stable_across_repeated_calls() -> None:
    given = [member(name) for name in ("A", "B", "C")]
    assert order_by_lane(order_by_lane(given)) == order_by_lane(given)


def test_the_board_presents_rows_in_lane_order() -> None:
    board, _clock, _calls = make_board(
        [positioned("Jinx", Role.BOTTOM), positioned("Ornn", Role.TOP)]
    )
    board.refresh()
    assert [row.champion for row in board.rows()][:2] == ["Ornn", "Jinx"]


# --- the countdown caption -------------------------------------------------


def test_the_first_displayed_second_is_not_inflated() -> None:
    """int(remaining)+1 showed N+1 at the instant of the press."""

    board, clock, _calls = make_board()
    board.refresh()
    board.press(0, CooldownSlot.SPELL_ONE)
    assert board.rows()[0].slots[1].caption == "300"


def test_the_caption_is_a_ceiling_while_counting() -> None:
    board, clock, _calls = make_board()
    board.refresh()
    board.press(0, CooldownSlot.SPELL_ONE)
    clock.advance(0.25)
    assert board.rows()[0].slots[1].caption == "300"
    clock.advance(0.75)
    assert board.rows()[0].slots[1].caption == "299"


def test_the_last_second_still_reads_one() -> None:
    board, clock, _calls = make_board()
    board.refresh()
    board.press(0, CooldownSlot.SPELL_ONE)
    clock.advance(299.5)
    assert board.rows()[0].slots[1].caption == "1"
    clock.advance(0.5)
    assert board.rows()[0].slots[1].caption == "up"


# --- icon paths reach the view --------------------------------------------


def test_icon_paths_are_carried_through_to_the_views() -> None:
    portrait = Path("C:/cache/icons/16.16.1/champion/Zed.png")
    spell = Path("C:/cache/icons/16.16.1/spell/ZedR.png")

    def resolve(people: Any) -> tuple[EnemyCooldownLoadout, ...]:
        return tuple(
            EnemyCooldownLoadout(
                participant_id=m.participant_id,
                champion_name=m.champion_name,
                champion_icon_path=portrait,
                ultimate=CooldownDefinition(
                    identifier="R",
                    display_name="R",
                    icon_path=spell,
                    cooldowns=(120.0, 100.0, 80.0),
                    max_rank=3,
                    unsupported_reason=None,
                ),
                summoner_spells=(definition("Flash", (300.0,)), unsupported("Smite")),
            )
            for m in people
        )

    board, _clock, _calls = make_board(resolver=resolve)
    board.refresh()
    row = board.rows()[0]
    assert row.champion_icon_path == portrait
    assert row.slots[0].icon_path == spell


def test_a_placeholder_row_carries_no_icon() -> None:
    board, _clock, _calls = make_board([member("Zed")])
    board.refresh()
    assert board.rows()[MAX_ROWS - 1].champion_icon_path is None


# --- the roster poller -----------------------------------------------------


def test_the_poller_drives_refresh_off_the_calling_thread() -> None:
    board, _clock, calls = make_board()
    poller = RosterPoller(board, poll_seconds=0.01)
    assert poller.start() is True
    deadline = time.time() + 3.0
    while time.time() < deadline and calls["roster"] == 0:
        time.sleep(0.01)
    assert poller.stop(timeout=3.0) is True
    assert calls["roster"] >= 1


def test_a_slow_roster_never_blocks_reading_rows() -> None:
    """The stall that made the countdown jump: a poll on the paint thread."""

    started = threading.Event()

    def slow_roster() -> RosterResult:
        started.set()
        time.sleep(0.6)
        return RosterResult(RosterStatus.ACTIVE, (member("Zed"),))

    board = CooldownBoard(
        CooldownTimerStore(FakeClock(), None),
        roster=slow_roster,
        resolve=lambda people: tuple(loadout(m.champion_name) for m in people),
    )
    poller = RosterPoller(board, poll_seconds=0.01)
    poller.start()
    try:
        assert started.wait(2.0)
        began = time.monotonic()
        board.rows()
        assert time.monotonic() - began < 0.2
    finally:
        poller.stop(timeout=3.0)


def test_a_raising_roster_does_not_kill_the_poller() -> None:
    def explode() -> RosterResult:
        raise OSError("the live client went away")

    board = CooldownBoard(
        CooldownTimerStore(FakeClock(), None), roster=explode, resolve=lambda people: ()
    )
    poller = RosterPoller(board, poll_seconds=0.01)
    assert poller.poll_once() is False
    assert poller.start() is True
    assert poller.stop(timeout=3.0) is True


def test_starting_the_poller_twice_is_refused() -> None:
    board, _clock, _calls = make_board()
    poller = RosterPoller(board, poll_seconds=0.01)
    poller.start()
    try:
        assert poller.start() is False
    finally:
        poller.stop(timeout=3.0)


def test_stopping_a_poller_that_never_started_is_harmless() -> None:
    board, _clock, _calls = make_board()
    assert RosterPoller(board).stop() is True


@pytest.mark.parametrize("poll", [0, -1])
def test_an_invalid_poll_interval_is_rejected(poll: float) -> None:
    board, _clock, _calls = make_board()
    with pytest.raises(ValueError, match="poll_seconds must be positive"):
        RosterPoller(board, poll_seconds=poll)
