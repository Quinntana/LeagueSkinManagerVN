"""Tests for the cooldown overlay window.

These build a real Tk interpreter and drive the real widget tree, because the
defects worth catching here are ones a mock cannot express: a release that
persists a position without a drag, a right click that reaches a timer, an
image that Tk garbage-collects into a blank square.

Skipped wherever Tk cannot open a display.
"""

from __future__ import annotations

import gc
import struct
import zlib
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from league_skin_manager.cooldown.board import CooldownBoard
from league_skin_manager.cooldown.panel import CooldownWindow, _next
from league_skin_manager.cooldown.roster import RosterMember, RosterResult, RosterStatus
from league_skin_manager.cooldown.timer import (
    CooldownDefinition,
    CooldownSlot,
    CooldownTimerStore,
    EnemyCooldownLoadout,
)

tk = pytest.importorskip("tkinter")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def timestamp(self) -> float:
        return 1_760_000_000.0 + self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class StillOverlay:
    """An overlay double that never hides the window and never styles it."""

    def __init__(self, foreground: bool | None = True) -> None:
        self.foreground = foreground
        self.applied: list[int] = []

    def apply(self, handle: int) -> bool:
        self.applied.append(handle)
        return True

    def is_foreground(self, _process: str) -> bool | None:
        return self.foreground


def png(size: int = 32) -> bytes:
    """A real, minimal, opaque PNG. Tk refuses anything less."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x80\x20\x40" * size for _ in range(size))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def definition(name: str, icon: Path | None = None) -> CooldownDefinition:
    return CooldownDefinition(
        identifier=name,
        display_name=name,
        icon_path=icon,
        cooldowns=(120.0, 100.0, 80.0),
        max_rank=3,
        unsupported_reason=None,
    )


def make_board(clock: Clock, *, portrait: Path | None = None, icon: Path | None = None) -> Any:
    member = RosterMember(champion_name="Ahri", participant_id="p1", champion_id="Ahri", level=11)

    def resolve(people: Any) -> tuple[EnemyCooldownLoadout, ...]:
        return tuple(
            EnemyCooldownLoadout(
                participant_id=m.participant_id,
                champion_name=m.champion_name,
                champion_icon_path=portrait,
                ultimate=definition("AhriR", icon),
                summoner_spells=(definition("Flash", icon), definition("Ignite", icon)),
            )
            for m in people
        )

    return CooldownBoard(
        CooldownTimerStore(clock, None),
        roster=lambda: RosterResult(RosterStatus.ACTIVE, (member,)),
        resolve=resolve,
    )


@pytest.fixture(scope="session")
def interpreter() -> Any:
    """One hidden Tk interpreter for the whole session.

    Creating a fresh ``tk.Tk()`` per test spawns a separate interpreter each
    time, which intermittently fails to initialise on Windows. One root and a
    Toplevel per test is both faster and stable.
    """

    try:
        root = tk.Tk()
    except tk.TclError:  # pragma: no cover - headless machine
        pytest.skip("Tk cannot open a display")
    root.withdraw()
    try:
        yield root
    finally:
        with suppress(Exception):
            root.destroy()


@pytest.fixture
def panel(request: pytest.FixtureRequest, interpreter: Any) -> Any:
    """A built, painted window that never enters mainloop."""

    options = getattr(request, "param", {}) or {}
    clock = Clock()
    board = make_board(clock, portrait=options.get("portrait"), icon=options.get("icon"))
    board.refresh()

    root = tk.Toplevel(interpreter)
    root.update()

    moved: list[tuple[int, int]] = []
    display: list[tuple[float, float]] = []
    closed: list[bool] = []
    window = CooldownWindow(
        board,
        opacity=0.85,
        scale=1.0,
        opacity_choices=(0.85, 0.55, 0.30),
        scale_choices=(1.0, 0.70),
        on_closed=lambda: closed.append(True),
        on_display=lambda o, s: display.append((o, s)),
        on_move=lambda left, top: moved.append((left, top)),
        overlay=StillOverlay(),
    )
    window._window = root
    window._build(root, tk)
    window._paint()
    root.update_idletasks()
    try:
        yield {
            "window": window,
            "root": root,
            "board": board,
            "clock": clock,
            "moved": moved,
            "display": display,
            "closed": closed,
        }
    finally:
        window._window = None
        window._rows.clear()
        window._images.clear()
        with suppress(Exception):
            root.destroy()
        # Collect here, deliberately. Dropping the last reference to a
        # PhotoImage makes tkinter call "image delete" on the interpreter; if
        # that happens at an arbitrary GC point during the next test it can
        # interleave with an "image create" on the same shared interpreter.
        gc.collect()


def event(x_root: int = 0, y_root: int = 0) -> Any:
    return type("Event", (), {"x_root": x_root, "y_root": y_root, "x": 0, "y": 0})()


# --- window chrome ---------------------------------------------------------


def test_the_window_has_no_title_bar(panel: Any) -> None:
    assert bool(panel["root"].overrideredirect()) is True


def test_the_window_is_topmost(panel: Any) -> None:
    assert bool(panel["root"].attributes("-topmost")) is True


def test_the_overlay_style_is_applied_to_the_window(panel: Any) -> None:
    assert panel["window"]._overlay.applied, "the non-activating style must be requested"


def test_the_board_carries_exactly_three_controls_and_one_readout(panel: Any) -> None:
    head = panel["window"]._body.winfo_children()[0]
    labels = [w for w in head.winfo_children() if isinstance(w, tk.Label)]
    texts = [w.cget("text") for w in labels]
    assert sorted(t for t in texts if t in {"x", "S", "O"}) == ["O", "S", "x"]
    assert len(labels) == 4, "three controls and the readout, nothing more"


def test_the_readout_shows_opacity_and_scale(panel: Any) -> None:
    assert panel["window"]._readout.cget("text") == "0.85  1x"


# --- clicks ---------------------------------------------------------------


def test_left_click_starts_a_timer(panel: Any) -> None:
    panel["window"]._pressed(0, CooldownSlot.ULTIMATE)
    assert panel["board"].rows()[0].slots[0].counting is True


def test_left_click_again_cancels_it(panel: Any) -> None:
    panel["window"]._pressed(0, CooldownSlot.ULTIMATE)
    panel["window"]._pressed(0, CooldownSlot.ULTIMATE)
    assert panel["board"].rows()[0].slots[0].counting is False


def test_right_click_is_bound_to_nothing_that_acts(panel: Any) -> None:
    """Right click is how a player moves; a stray one must cost nothing."""

    window = panel["window"]
    slot = window._rows[0]["slots"][CooldownSlot.ULTIMATE]
    for widget in (panel["root"], slot):
        bound = widget.bind("<Button-3>")
        assert bound, "right click must be explicitly swallowed"
    window._pressed(0, CooldownSlot.ULTIMATE)
    before = panel["board"].rows()[0].slots[0].caption
    slot.event_generate("<Button-3>")
    panel["root"].update()
    assert panel["board"].rows()[0].slots[0].caption == before
    assert panel["closed"] == [], "right click must never close the board"


# --- dragging --------------------------------------------------------------


def test_a_click_without_movement_persists_nothing(panel: Any) -> None:
    """Otherwise every use of the corner controls rewrites the position."""

    window = panel["window"]
    window._drag_start(event(100, 100))
    window._drag_move(event(101, 100))
    window._drag_end(event(101, 100))
    assert panel["moved"] == []


def test_a_real_drag_persists_the_position(panel: Any) -> None:
    window = panel["window"]
    window._drag_start(event(100, 100))
    window._drag_move(event(240, 260))
    panel["root"].update_idletasks()
    window._drag_end(event(240, 260))
    assert len(panel["moved"]) == 1


def test_a_restored_position_is_clamped_to_the_screen(panel: Any) -> None:
    window = panel["window"]
    left, top = window._clamped(999_999, 999_999)
    assert left <= panel["root"].winfo_screenwidth()
    assert top <= panel["root"].winfo_screenheight()
    assert (left, top) >= (0, 0)


def test_a_negative_position_is_clamped_to_the_origin(panel: Any) -> None:
    assert panel["window"]._clamped(-500, -500) == (0, 0)


# --- display controls ------------------------------------------------------


def test_cycling_opacity_persists_and_updates_the_readout(panel: Any) -> None:
    window = panel["window"]
    window._cycle_opacity()
    assert window._opacity == 0.55
    assert panel["display"] == [(0.55, 1.0)]
    assert window._readout.cget("text") == "0.55  1x"


def test_opacity_wraps_around(panel: Any) -> None:
    window = panel["window"]
    for _ in range(3):
        window._cycle_opacity()
    assert window._opacity == 0.85


def test_cycling_scale_rebuilds_and_persists(panel: Any) -> None:
    window = panel["window"]
    before = window._metrics()["icon"]
    window._cycle_scale()
    assert window._scale == 0.70
    assert window._metrics()["icon"] < before, "scale must move pixels, not only fonts"
    assert panel["display"] == [(0.85, 0.70)]


def test_scale_moves_icon_padding_and_font_together(panel: Any) -> None:
    window = panel["window"]
    big = window._metrics()
    window._scale = 0.70
    small = window._metrics()
    assert small["icon"] < big["icon"]
    assert small["pad"] <= big["pad"]
    assert small["cell"] < big["cell"]


def test_an_unknown_stored_preset_falls_back_to_the_first() -> None:
    assert _next((1.0, 0.85), 0.42) == 1.0


def test_next_tolerates_an_empty_preset_list() -> None:
    assert _next((), 0.85) == 0.85


# --- foreground binding ----------------------------------------------------


def test_the_board_hides_when_the_game_is_not_in_front(panel: Any) -> None:
    window = panel["window"]
    window._overlay.foreground = False
    window._follow_foreground()
    panel["root"].update_idletasks()
    assert window._hidden_for_foreground is True


def test_the_board_returns_when_the_game_comes_back(panel: Any) -> None:
    window = panel["window"]
    window._overlay.foreground = False
    window._follow_foreground()
    window._overlay.foreground = True
    window._follow_foreground()
    assert window._hidden_for_foreground is False


def test_timers_keep_running_while_hidden(panel: Any) -> None:
    window = panel["window"]
    window._pressed(0, CooldownSlot.ULTIMATE)
    window._overlay.foreground = False
    window._follow_foreground()
    panel["clock"].advance(5.0)
    # Level 11 is ultimate rank 2, so 100s, not the rank-3 80s.
    assert panel["board"].rows()[0].slots[0].caption == "95"


def test_an_unknown_foreground_leaves_the_board_alone(panel: Any) -> None:
    """None means 'could not tell', not 'the game is gone'."""

    window = panel["window"]
    window._overlay.foreground = None
    window._follow_foreground()
    assert window._hidden_for_foreground is False


# --- icons -----------------------------------------------------------------


@pytest.mark.parametrize("panel", [{"portrait": None, "icon": None}], indirect=True)
def test_a_row_without_a_portrait_shows_the_champion_name(panel: Any) -> None:
    assert panel["window"]._rows[0]["face"].cget("text") == "Ahri"


def test_an_icon_is_loaded_and_its_reference_held(tmp_path: Path, panel: Any) -> None:
    """Tk collects an image whose last Python reference drops, drawing nothing."""

    art = tmp_path / "Ahri.png"
    art.write_bytes(png(32))
    window = panel["window"]
    image = window._image(art, 32)
    assert image is not None
    assert window._image(art, 32) is image, "a second call must reuse the held reference"


def test_an_icon_is_subsampled_to_the_requested_size(tmp_path: Path, panel: Any) -> None:
    art = tmp_path / "portrait.png"
    art.write_bytes(png(128))
    image = panel["window"]._image(art, 32)
    assert image is not None
    assert image.width() == 32


def test_a_missing_icon_file_falls_back_to_none(tmp_path: Path, panel: Any) -> None:
    assert panel["window"]._image(tmp_path / "absent.png", 32) is None


def test_a_corrupt_icon_falls_back_to_none(tmp_path: Path, panel: Any) -> None:
    art = tmp_path / "broken.png"
    art.write_bytes(b"\x89PNG\r\n\x1a\nnot really a png")
    assert panel["window"]._image(art, 32) is None


def test_no_icon_path_means_no_image(panel: Any) -> None:
    assert panel["window"]._image(None, 32) is None


# --- how a slot is painted -------------------------------------------------


def items(canvas: Any) -> list[tuple[str, dict[str, Any]]]:
    """Every drawn item as (kind, interesting options)."""

    out = []
    for item in canvas.find_all():
        kind = canvas.type(item)
        options: dict[str, Any] = {}
        if kind == "rectangle":
            options["stipple"] = str(canvas.itemcget(item, "stipple"))
            options["fill"] = str(canvas.itemcget(item, "fill"))
        elif kind == "line":
            options["fill"] = str(canvas.itemcget(item, "fill"))
        elif kind == "text":
            options["text"] = str(canvas.itemcget(item, "text"))
            options["fill"] = str(canvas.itemcget(item, "fill"))
        out.append((kind, options))
    return out


@pytest.fixture
def painted(tmp_path: Path, panel: Any) -> Any:
    """A panel whose slots have real icons, so the art paths are exercised."""

    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    window = panel["window"]
    window._board = make_board(panel["clock"], portrait=art, icon=art)
    window._board.refresh()
    window._paint()
    panel["root"].update_idletasks()
    return panel


def slot_of(panel: Any) -> Any:
    return panel["window"]._rows[0]["slots"][CooldownSlot.ULTIMATE]


def test_an_idle_slot_draws_its_icon_and_no_number(painted: Any) -> None:
    drawn = items(slot_of(painted))
    assert [kind for kind, _ in drawn] == ["image"]


def test_a_counting_slot_dims_its_icon(painted: Any) -> None:
    """gray50 is Tk's 50% dither -- fake transparency with no dependency."""

    painted["window"]._pressed(0, CooldownSlot.ULTIMATE)
    drawn = items(slot_of(painted))
    rectangles = [options for kind, options in drawn if kind == "rectangle"]
    assert len(rectangles) == 1
    assert rectangles[0]["stipple"] == "gray50"
    assert rectangles[0]["fill"] == "#000000"


def test_a_counting_slot_keeps_its_icon_visible(painted: Any) -> None:
    """The two summoner slots are told apart by their art, not by position."""

    painted["window"]._pressed(0, CooldownSlot.ULTIMATE)
    assert any(kind == "image" for kind, _ in items(slot_of(painted)))


def test_the_number_is_outlined_so_it_survives_a_faint_board(painted: Any) -> None:
    painted["window"]._pressed(0, CooldownSlot.ULTIMATE)
    texts = [options for kind, options in items(slot_of(painted)) if kind == "text"]
    assert len(texts) == 9, "eight black offsets plus the white face"
    assert [t["fill"] for t in texts].count("#000000") == 8
    assert [t["fill"] for t in texts].count("#ffffff") == 1
    assert {t["text"] for t in texts} == {"100"}


def test_a_ready_slot_says_up_and_is_not_dimmed(painted: Any) -> None:
    painted["window"]._pressed(0, CooldownSlot.ULTIMATE)
    painted["clock"].advance(1000.0)
    painted["window"]._paint()
    drawn = items(slot_of(painted))
    assert not any(kind == "rectangle" for kind, _ in drawn)
    texts = [options for kind, options in drawn if kind == "text"]
    assert [t["text"] for t in texts] == ["up"]


def test_a_slot_without_art_falls_back_to_its_letter(panel: Any) -> None:
    drawn = items(slot_of(panel))
    texts = [options for kind, options in drawn if kind == "text"]
    assert [t["text"] for t in texts] == ["R"]


# --- settings arriving from the tray ---------------------------------------


def test_a_tray_opacity_change_applies_without_a_rebuild(panel: Any) -> None:
    window = panel["window"]
    body = window._body
    window.set_display(opacity=0.30, scale=1.0)
    panel["root"].update()
    assert window._opacity == 0.30
    assert window._body is body, "opacity alone must not re-lay the board"


def test_a_tray_scale_change_rebuilds_the_board(panel: Any) -> None:
    window = panel["window"]
    body = window._body
    window.set_display(opacity=0.85, scale=0.70)
    panel["root"].update()
    assert window._body is not body


def test_set_display_on_a_closed_window_is_harmless(panel: Any) -> None:
    window = panel["window"]
    window._window = None
    window.set_display(opacity=0.30, scale=0.70)
    assert window._opacity == 0.30


def test_a_failed_icon_is_not_cached_forever(tmp_path: Path, panel: Any) -> None:
    """A transient Tk failure must not blank that square for the whole match."""

    art = tmp_path / "later.png"
    window = panel["window"]

    assert window._image(art, 32) is None
    assert (str(art), 32) not in window._images, "a failure must not be remembered"

    art.write_bytes(png(32))
    assert window._image(art, 32) is not None, "the next repaint must try again"


def test_a_successful_icon_is_cached(tmp_path: Path, panel: Any) -> None:
    art = tmp_path / "cached.png"
    art.write_bytes(png(32))
    window = panel["window"]
    window._image(art, 32)
    assert (str(art), 32) in window._images


# --- an ability that is not available yet ----------------------------------


def unavailable_board(clock: Clock, icon: Path | None) -> Any:
    """An enemy at level 5: a three-rank ultimate is not learned yet."""

    member = RosterMember(champion_name="Ahri", participant_id="p1", champion_id="Ahri", level=5)

    def resolve(people: Any) -> tuple[EnemyCooldownLoadout, ...]:
        return tuple(
            EnemyCooldownLoadout(
                participant_id=m.participant_id,
                champion_name=m.champion_name,
                champion_icon_path=icon,
                ultimate=definition("AhriR", icon),
                summoner_spells=(definition("Flash", icon), definition("Ignite", icon)),
            )
            for m in people
        )

    return CooldownBoard(
        CooldownTimerStore(clock, None),
        roster=lambda: RosterResult(RosterStatus.ACTIVE, (member,)),
        resolve=resolve,
    )


def test_an_unlearned_ultimate_is_marked_unavailable(tmp_path: Path, panel: Any) -> None:
    """Level 5: a three-rank ultimate is not learned, and must not look ready."""

    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    window = panel["window"]
    window._board = unavailable_board(panel["clock"], art)
    window._board.refresh()
    window._paint()

    drawn = items(slot_of(panel))
    assert not [o for kind, o in drawn if kind == "rectangle"], "the icon must stay readable"
    lines = [o for kind, o in drawn if kind == "line"]
    assert len(lines) == 4, "a cross, drawn dark then red"
    assert [o["fill"] for o in lines].count("#ef4444") == 2
    assert not [o for kind, o in drawn if kind == "text"], "no number on an unlearned ability"


def test_unavailable_and_counting_are_dimmed_differently(tmp_path: Path, panel: Any) -> None:
    """Both are dark; they must not be indistinguishable."""

    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    window = panel["window"]

    window._board = unavailable_board(panel["clock"], art)
    window._board.refresh()
    window._paint()
    unavailable = [kind for kind, _ in items(slot_of(panel))]

    window._board = make_board(panel["clock"], portrait=art, icon=art)
    window._board.refresh()
    window._paint()
    window._pressed(0, CooldownSlot.ULTIMATE)
    counting = [kind for kind, _ in items(slot_of(panel))]

    assert "line" in unavailable and "line" not in counting
    assert "rectangle" in counting and "rectangle" not in unavailable


def test_an_available_ultimate_is_not_dimmed(tmp_path: Path, panel: Any) -> None:
    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    window = panel["window"]
    window._board = make_board(panel["clock"], portrait=art, icon=art)
    window._board.refresh()
    window._paint()
    drawn = items(slot_of(panel))
    assert not [o for kind, o in drawn if kind == "rectangle"]
    assert not [o for kind, o in drawn if kind == "line"], "an available slot carries no cross"


def test_a_charge_based_spell_is_dimmed_too(tmp_path: Path, panel: Any) -> None:
    """Smite and friends are unsupported, not merely off cooldown."""

    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    member = RosterMember(champion_name="Ahri", participant_id="p1", champion_id="Ahri", level=18)
    smite = CooldownDefinition(
        identifier="SummonerSmite",
        display_name="Smite",
        icon_path=art,
        cooldowns=(),
        max_rank=0,
        unsupported_reason="Smite uses charge-based cooldown behaviour",
    )

    def resolve(people: Any) -> tuple[EnemyCooldownLoadout, ...]:
        return tuple(
            EnemyCooldownLoadout(
                participant_id=m.participant_id,
                champion_name=m.champion_name,
                champion_icon_path=art,
                ultimate=definition("AhriR", art),
                summoner_spells=(smite, definition("Flash", art)),
            )
            for m in people
        )

    window = panel["window"]
    window._board = CooldownBoard(
        CooldownTimerStore(panel["clock"], None),
        roster=lambda: RosterResult(RosterStatus.ACTIVE, (member,)),
        resolve=resolve,
    )
    window._board.refresh()
    window._paint()

    smite_canvas = window._rows[0]["slots"][CooldownSlot.SPELL_ONE]
    lines = [o for kind, o in items(smite_canvas) if kind == "line"]
    assert len(lines) == 4
    assert [o["fill"] for o in lines].count("#ef4444") == 2


# --- the crash on reopen ---------------------------------------------------


def test_teardown_drops_every_reference_to_the_interpreter(panel: Any) -> None:
    """Tcl aborts the process if the last Tk reference dies on another thread.

    close_panel runs on the tray thread, so a single surviving widget
    attribute here kills the application with no traceback and nothing in the
    log. This is what crashed a real reopen.
    """

    window = panel["window"]
    assert window._body is not None and window._readout is not None

    window._release()

    assert window._window is None
    assert window._body is None
    assert window._readout is None
    assert window._rows == []
    assert window._images == {}


# --- hiding is not ending ---------------------------------------------------


def test_a_new_board_is_visible(panel: Any) -> None:
    assert panel["window"].is_visible is True


def test_hiding_withdraws_without_ending_the_session(panel: Any) -> None:
    window = panel["window"]
    window.hide()
    panel["root"].update()
    assert window.is_visible is False
    assert window._closing is False, "hiding must not ask the loop to exit"
    assert window._window is not None, "the session and its timers survive"


def test_timers_keep_counting_while_hidden(panel: Any) -> None:
    window = panel["window"]
    window._pressed(0, CooldownSlot.ULTIMATE)
    window.hide()
    panel["root"].update()
    panel["clock"].advance(10.0)
    assert panel["board"].rows()[0].slots[0].caption == "90"


def test_showing_again_restores_the_same_board(panel: Any) -> None:
    window = panel["window"]
    window._pressed(0, CooldownSlot.ULTIMATE)
    window.hide()
    panel["root"].update()
    panel["clock"].advance(4.0)
    window.show()
    panel["root"].update()
    assert window.is_visible is True
    assert panel["board"].rows()[0].slots[0].caption == "96"


def test_a_deliberate_hide_survives_leaving_and_returning_to_the_game(panel: Any) -> None:
    """The bug this two-flag design exists to prevent."""

    window = panel["window"]
    window.hide()
    panel["root"].update()

    window._overlay.foreground = False
    window._follow_foreground()
    window._overlay.foreground = True
    window._follow_foreground()
    panel["root"].update()

    assert window.is_visible is False, "returning to the game must not undo a hide"


def test_the_foreground_watcher_only_clears_its_own_flag(panel: Any) -> None:
    window = panel["window"]
    window.hide()
    panel["root"].update()
    window._overlay.foreground = False
    window._follow_foreground()
    window._overlay.foreground = True
    window._follow_foreground()
    assert window._hidden_by_user is True
    assert window._hidden_for_foreground is False


def test_hiding_notifies_only_on_a_change(panel: Any) -> None:
    seen: list[bool] = []
    window = panel["window"]
    window._on_hidden = seen.append

    window.hide()
    window.hide()
    window.show()
    panel["root"].update()

    assert seen == [True, False], "repeated hides must not re-notify"


def test_losing_foreground_does_not_notify_the_shell(panel: Any) -> None:
    """Otherwise the tray entry would flicker every time you alt-tab."""

    seen: list[bool] = []
    window = panel["window"]
    window._on_hidden = seen.append
    window._overlay.foreground = False
    window._follow_foreground()
    panel["root"].update()
    assert seen == []


def test_painting_is_skipped_while_hidden(panel: Any) -> None:
    window = panel["window"]
    window.hide()
    panel["root"].update()

    slot = slot_of(panel)
    slot.delete("all")
    window._paint()
    assert slot.find_all() == (), "no canvas work while off screen"


def test_showing_paints_immediately(panel: Any) -> None:
    """The board must never appear holding stale numbers."""

    window = panel["window"]
    window.hide()
    panel["root"].update()
    slot_of(panel).delete("all")

    window.show()
    panel["root"].update()

    assert slot_of(panel).find_all() != ()


# --- redraw only what changed ----------------------------------------------


def test_an_unchanged_slot_is_not_redrawn(tmp_path: Path, panel: Any) -> None:
    """The flicker: repainting a translucent layered window re-blends all of it."""

    art = tmp_path / "icon.png"
    art.write_bytes(png(64))
    window = panel["window"]
    window._board = make_board(panel["clock"], portrait=art, icon=art)
    window._board.refresh()
    window._paint()

    slot = slot_of(panel)
    slot.delete("all")
    window._paint()

    assert slot.find_all() == (), "nothing changed, so nothing may be redrawn"


def test_a_changed_slot_is_redrawn(panel: Any) -> None:
    window = panel["window"]
    window._paint()
    slot = slot_of(panel)
    slot.delete("all")

    window._pressed(0, CooldownSlot.ULTIMATE)

    assert slot.find_all() != (), "starting a timer must repaint that slot"


def test_only_the_clicked_slot_is_redrawn(panel: Any) -> None:
    """A click used to repaint all fifteen slots, which is what flashed."""

    window = panel["window"]
    window._paint()
    clicked = window._rows[0]["slots"][CooldownSlot.ULTIMATE]
    untouched = window._rows[3]["slots"][CooldownSlot.SPELL_ONE]
    clicked.delete("all")
    untouched.delete("all")

    window._pressed(0, CooldownSlot.ULTIMATE)

    assert clicked.find_all() != ()
    assert untouched.find_all() == (), "an unrelated slot must be left alone"


def test_the_countdown_still_repaints_every_second(panel: Any) -> None:
    window = panel["window"]
    window._pressed(0, CooldownSlot.ULTIMATE)
    window._paint()
    slot = slot_of(panel)
    slot.delete("all")

    panel["clock"].advance(1.5)
    window._paint()

    assert slot.find_all() != (), "the caption changed, so it must be redrawn"


def test_showing_the_board_discards_the_cached_state(panel: Any) -> None:
    window = panel["window"]
    window._paint()
    window.hide()
    panel["root"].update()
    slot_of(panel).delete("all")

    window.show()
    panel["root"].update()

    assert slot_of(panel).find_all() != (), "a shown board must not stay blank"
