## Why

The board's lifetime is currently tied to its window: closing it destroys the
Tk interpreter, the timer store, the catalog, and the roster poller, and
re-opening builds all of them again. That is the wrong unit. A player has one
board per game, not one per time they glance at it.

It is also the direct cause of a crash found in live play. Re-opening creates a
**second `tk.Tk()` in one process**, and Tcl aborts the process outright —
`Tcl_AsyncDelete: async handler deleted by the wrong thread` — if any reference
to the first interpreter outlives its thread. That abort produces no exception
and no log line; the application simply vanishes. It has been fixed once by
hand, but the shape of the design keeps the hazard available.

Binding the board's lifetime to the match instead of to the window removes the
hazard rather than guarding it, and gives the behaviour the player expects:
timers that keep running while the board is out of the way.

## What Changes

- The board is created once per game and destroyed when the game exits. Opening
  and closing it show and hide one window; they no longer construct or tear
  down anything.
- **BREAKING:** closing the board no longer discards its timers. They continue
  running while it is hidden, and are still there when it is shown again. This
  reverses a requirement added in `cooldown-overlay`.
- Timers, roster polling, and the cooldown catalog live for the length of the
  match rather than the length of the window.
- The tray's cooldown entry may be used to show the board again after hiding
  it, at any point during the match.
- Everything is released when `League of Legends.exe` exits, however it exits —
  a normal end, a crash, or the machine losing power, which releases it by
  releasing the process.
- **Removed:** the suppress-re-opening-for-this-match flag in the composition
  root. With hide and show there is nothing to suppress.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cooldown-board`: amends "The board follows the match" so the board's lifetime
  is the match rather than the window, closing hides rather than destroys, and
  timers survive being hidden.

## Impact

**Code**

- `cooldown/__init__.py` — `open_panel` shows an existing board instead of
  building a second one; `close_panel` hides; a new function releases the
  session. The package's public surface grows from four functions to five,
  which `tests/test_architecture.py` pins.
- `cooldown/panel.py` — `hide` withdraws instead of quitting; the panel
  distinguishes hidden-by-the-user from hidden-because-the-game-is-not-in-front,
  and skips painting while hidden.
- `app.py` — the game-stopped transition releases the session; `_suppressed_for_match`
  is deleted.
- `cooldown/host.py` — unchanged.

**Risk**

The interface thread now lives for the whole match rather than for the time the
board is on screen. That is a withdrawn window and a timer tick; painting is
skipped while hidden.

## Measured versus inferred

Measured on this machine, 2026-08-20:

- Re-opening the board after closing it aborted the process with
  `Tcl_AsyncDelete: async handler deleted by the wrong thread`, reproducible on
  the first cycle. Bisected: a Tk interpreter created and destroyed wholly on
  one worker thread survives repeated cycles; the same with widgets aborts at
  exit; **a surviving reference released on another thread aborts immediately**.
- After clearing every widget attribute on the owning thread, three
  open-close-open cycles complete cleanly.

Inferred, not measured:

- That keeping one interpreter alive for a whole match costs nothing
  noticeable. It is a withdrawn window and a 250 ms tick that paints nothing,
  but it has not been profiled over a full game.

## Rejected alternatives

- **Leaving the current create-and-destroy design and relying on the manual
  fix.** The fix is one line of discipline — clear every widget attribute on
  the owning thread — enforced by a single test. Any future attribute added to
  the panel re-arms the same process-killing abort, with no exception and no
  log to diagnose it from. The hazard should be removed, not documented.
- **Keeping one Tk interpreter alive for the whole application, not just the
  match.** It would remove the hazard equally well, but it puts a Tk event loop
  in a tray-only application that may never open a board, and it keeps the
  catalog and roster poller alive between games for no purpose.
- **Persisting timers across a game so they survive a crash.** Timers are
  monotonic and a crash loses the process anyway; writing them to disk would
  add state that has to be invalidated, for a case where the correct answer is
  to start clean.
