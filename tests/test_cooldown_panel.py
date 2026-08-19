from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.cooldown.panel import (
    MAX_MANUAL_SECONDS,
    MIN_MANUAL_SECONDS,
    ROLE_LABELS,
    SUMMONER_SPELL_PRESETS,
    CooldownBoard,
    format_slot_text,
    manual_definition,
    preset_by_label,
)
from league_skin_manager.cooldown.timer import (
    CooldownKey,
    CooldownSlot,
    CooldownSnapshot,
    CooldownTimerStore,
    CsvCooldownEventSink,
)


class Clock:
    def __init__(self) -> None:
        self.now = 500.0

    def monotonic(self) -> float:
        return self.now

    def timestamp(self) -> str:
        return "2026-07-27 12:00:00"


def board(clock: Clock, **options: Any) -> CooldownBoard:
    return CooldownBoard(
        CooldownTimerStore(clock, options.pop("recorder", None)),
        logger=logging.getLogger("test.cooldown_window"),
        **options,
    )


def test_manual_definition_produces_a_flat_one_rank_cooldown() -> None:
    definition = manual_definition("SummonerFlash", "Flash 300s", 300.0)

    assert definition.max_rank == 1
    assert definition.duration_for_level(1) == 300.0
    assert definition.duration_for_level(18) == 300.0


@pytest.mark.parametrize(
    "seconds",
    [MIN_MANUAL_SECONDS - 1, MAX_MANUAL_SECONDS + 1, 0, -30, float("nan"), float("inf")],
)
def test_manual_definition_rejects_out_of_range_durations(seconds: float) -> None:
    with pytest.raises(ValueError, match="seconds must be between"):
        manual_definition("ManualUltimate", "Ultimate", seconds)


def test_every_preset_label_resolves_and_unknown_labels_do_not() -> None:
    for preset in SUMMONER_SPELL_PRESETS:
        assert preset_by_label(preset.label) is preset
        assert (
            manual_definition(preset.identifier, preset.label, preset.seconds).duration_for_level(1)
            == preset.seconds
        )
    assert preset_by_label("Not a spell") is None


def test_format_slot_text_rounds_up_and_marks_ready() -> None:
    key = CooldownKey("row-0", CooldownSlot.ULTIMATE)

    def snapshot(remaining: float) -> CooldownSnapshot:
        return CooldownSnapshot(
            key=key,
            champion_name="Top",
            identifier="ManualUltimate",
            display_name="Ultimate",
            icon_path=None,
            duration=120.0,
            level=1,
            started_at=0.0,
            ready_at=120.0,
            remaining=remaining,
            is_ready=remaining == 0.0,
        )

    assert format_slot_text("Ult", None) == "Ult"
    assert format_slot_text("Ult", snapshot(0.0)) == "Ult - ready"
    assert format_slot_text("Ult", snapshot(0.2)) == "Ult - 1s"
    assert format_slot_text("Ult", snapshot(59.4)) == "Ult - 60s"


def test_press_starts_a_timer_that_counts_down_and_becomes_ready() -> None:
    clock = Clock()
    value = board(clock)

    snapshot = value.press(
        0,
        CooldownSlot.ULTIMATE,
        identifier="ManualUltimate",
        display_name="Ultimate",
        seconds=120.0,
        champion="Aatrox",
    )

    assert snapshot is not None and snapshot.remaining == 120.0
    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult - 120s"
    clock.now += 119.5
    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult - 1s"
    clock.now += 0.5
    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult - ready"


def test_press_restarts_the_same_slot_at_full_duration() -> None:
    clock = Clock()
    value = board(clock)
    value.press(
        1,
        CooldownSlot.SPELL_ONE,
        identifier="SummonerFlash",
        display_name="Flash 300s",
        seconds=300.0,
        champion="Jungle",
    )
    clock.now += 200.0

    restarted = value.press(
        1,
        CooldownSlot.SPELL_ONE,
        identifier="SummonerFlash",
        display_name="Flash 300s",
        seconds=300.0,
        champion="Jungle",
    )

    assert restarted is not None and restarted.remaining == 300.0


def test_slots_and_rows_are_independent() -> None:
    clock = Clock()
    value = board(clock)
    value.press(
        0,
        CooldownSlot.ULTIMATE,
        identifier="ManualUltimate",
        display_name="Ultimate",
        seconds=100.0,
        champion="Top",
    )
    value.press(
        2,
        CooldownSlot.SPELL_TWO,
        identifier="SummonerDot",
        display_name="Ignite 180s",
        seconds=180.0,
        champion="Mid",
    )

    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult - 100s"
    assert value.text(2, CooldownSlot.SPELL_TWO, "Start") == "Start - 180s"
    assert value.text(0, CooldownSlot.SPELL_TWO, "Start") == "Start"
    assert value.text(1, CooldownSlot.ULTIMATE, "Ult") == "Ult"


def test_clear_and_clear_all_cancel_only_running_timers() -> None:
    clock = Clock()
    value = board(clock)
    for row in range(3):
        value.press(
            row,
            CooldownSlot.ULTIMATE,
            identifier="ManualUltimate",
            display_name="Ultimate",
            seconds=90.0,
            champion="Enemy",
        )

    assert value.clear(0, CooldownSlot.ULTIMATE) is True
    assert value.clear(0, CooldownSlot.ULTIMATE) is False
    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult"
    assert value.clear_all() == 2
    assert value.clear_all() == 0


def test_blank_champion_falls_back_to_the_row_role_label() -> None:
    clock = Clock()
    events: list[Any] = []

    class Recorder:
        def record(self, event: Any) -> None:
            events.append(event)

    value = board(clock, recorder=Recorder())
    value.press(
        3,
        CooldownSlot.ULTIMATE,
        identifier="ManualUltimate",
        display_name="Ultimate",
        seconds=90.0,
        champion="   ",
    )

    assert events[-1].champion_name == ROLE_LABELS[3]


def test_board_writes_events_through_the_csv_sink(tmp_path: Path) -> None:
    path = tmp_path / "cooldown-events.csv"
    sink = CsvCooldownEventSink(path)
    value = CooldownBoard(
        CooldownTimerStore(Clock(), sink, session_id="session-1"),
        flush=sink.flush,
        logger=logging.getLogger("test.cooldown_window"),
    )

    value.press(
        0,
        CooldownSlot.SPELL_ONE,
        identifier="SummonerFlash",
        display_name="Flash 300s",
        seconds=300.0,
        champion="Aatrox",
    )
    value.clear(0, CooldownSlot.SPELL_ONE)

    with path.open(newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))
    assert [row["action"] for row in rows] == ["start", "cancel"]
    assert rows[0]["participant"] == "row-0"
    assert rows[0]["session"] == "session-1"


def test_flush_failure_is_logged_without_losing_the_timer(
    tmp_path: Path,
    caplog: Any,
) -> None:
    def failing_flush() -> None:
        raise OSError("disk unavailable")

    value = CooldownBoard(
        CooldownTimerStore(Clock()),
        flush=failing_flush,
        logger=logging.getLogger("test.cooldown_window"),
    )
    caplog.set_level(logging.WARNING, logger="test.cooldown_window")

    snapshot = value.press(
        0,
        CooldownSlot.ULTIMATE,
        identifier="ManualUltimate",
        display_name="Ultimate",
        seconds=60.0,
        champion="Aatrox",
    )

    assert snapshot is not None
    assert value.text(0, CooldownSlot.ULTIMATE, "Ult") == "Ult - 60s"
    assert "Could not persist cooldown events" in caplog.text
