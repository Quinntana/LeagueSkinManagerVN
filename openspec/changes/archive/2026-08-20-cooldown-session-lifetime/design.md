# Design

## Context

See `proposal.md` — Why. Requirements are in `specs/`.

The board currently owns its Tk interpreter for the length of the *window*.
`open_panel` builds a `LiveClient`, a `CooldownCatalog`, a `CooldownBoard`, a
`CooldownTimerStore`, a `WindowHost`, a thread, a `tk.Tk()`, and a
`RosterPoller`; `close_panel` destroys all of it. Doing that twice in one
process is what aborts Tcl.

## Goals / Non-Goals

**Goals**

- One interpreter per match, created on the first open and released with the
  game process.
- No behaviour change to painting, icons, dragging, or the controls.

**Non-Goals**

- Persisting timers to disk.
- Keeping an interpreter alive between matches.
- Changing what a click does.

## Decisions

### D1 — The session is the unit, and `app.py` already tracks it

`GameWatcher` already reports the game process starting and stopping, and
`app.py` already calls `_close_cooldowns()` on the stop transition. That call
becomes the release point. Nothing new watches anything.

### D2 — Five public functions, not four

The package exposes `open_panel`, `close_panel`, `is_open`, `apply_display`.
Hiding and releasing are genuinely different operations with different
consequences, so they get different names rather than a boolean:

| Function | Meaning |
|---|---|
| `open_panel` | show the board, building the session if there is none |
| `close_panel` | hide the board; the session keeps running |
| `release_panel` | end the session and free everything |
| `is_open` | a session exists |
| `is_visible` | the board is on screen |
| `apply_display` | unchanged |

`is_open` and `is_visible` are genuinely different once hiding stops destroying
anything: between them they say which of the three states the board is in --
absent, hidden, or on screen. The shell needs the distinction to know whether
its open action should be offered.

`tests/test_architecture.py` pins the exported set, so it is updated
deliberately rather than incidentally.

### D2a -- one board per game, enforced at both ends

A game has one set of enemy cooldowns, so a second board could only disagree
with the first. While the board is on screen the tray's open action is
disabled, alongside the existing rule that disables it outside a match; hiding
the board re-enables it.

The menu state is a hint, not the guarantee. `open_panel` is itself a no-op when
the board is already visible, so no route -- a stale menu, an automatic open, a
future caller -- can produce a second board.

*Alternative rejected:* `close_panel(release=False)`. A boolean parameter that
switches between "hide this" and "destroy everything" is exactly the kind of
call that reads harmlessly at the call site and is catastrophic when wrong.

### D3 — Two hidden states, both in the panel

Hiding is currently one idea. It becomes two:

```
                     user hides            game loses focus
                          |                       |
                          v                       v
                 _hidden_by_user          _hidden_for_foreground
                          \                       /
                           \                     /
                            visible = neither is set
```

The foreground watcher may only clear its own flag. That is what stops
alt-tabbing back from undoing a deliberate hide, and it is why this cannot stay
as a single boolean.

`app.py` loses `_suppressed_for_match` entirely: with a persistent session
there is no re-open to suppress, because opening an already-built board is just
`deiconify`.

### D4 — Painting stops while hidden

The repaint tick keeps running so the schedule stays anchored, but `_paint`
returns immediately when the board is not visible. Timers are read from a
monotonic clock on demand, so nothing needs to accumulate while hidden — the
next paint reads the true remaining time with no catch-up.

### D5 — Release still happens on the owning thread

`release_panel` asks the window to quit; `run()`'s `finally` does the teardown,
including `_release()`, on the thread that created the interpreter. That
discipline stays exactly as it is — this change means it happens once per
match instead of once per glance, which is what makes the remaining hazard
small enough to live with.

## Risks / Trade-offs

- **A session that never gets released** if the game-stopped transition is
  missed → `GameWatcher` treats a failed poll as "no change" rather than "the
  game stopped", so a transient failure cannot leak a session, and a real exit
  is still seen on the next poll. Application shutdown releases it regardless.
- **An interpreter alive for a whole match** → a withdrawn window and a tick
  that paints nothing. Not profiled over a full game; noted as inferred.
- **Reversing a shipped requirement** — `cooldown-overlay` states that closing
  discards timers, and this states the opposite → captured as a `MODIFIED`
  requirement so the reversal is visible in the spec history rather than being
  a silent contradiction between two changes.

## Migration Plan

None. No persisted state changes shape, and no settings key is added or
removed.

## Open Questions

None.
