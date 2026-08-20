## Why

The cooldown board reached a real match for the first time and four things
failed at once: it draws as an ordinary titled window that cannot appear over
the game, it has no icons, its countdown drifts and stalls, and once open there
is no way to close it. Separately, the one LTK setting this application claims
to manage — `enforceSkinhackScan` — is still `true` after a full first run.

All five are presentation or ordering defects in shipped v4.0.0 behaviour, not
new capability. None of them are visible from unit tests, which is why they
survived to a release.

## What Changes

**Overlay presentation**

- The board loses its title bar and becomes a borderless, non-activating,
  always-on-top window. Dragging anywhere that is not a spell square moves it;
  its position persists across launches.
- The board hides itself whenever `League of Legends.exe` is not the foreground
  window, and reappears when it returns. It no longer appears over the desktop
  or over other applications during a match.
- The board opens see-through rather than as a solid panel, so the game behind it
  stays visible, and the tray gains a lower opacity setting to go with the new
  default.
- **BREAKING (behavioural):** the board can only draw over League in Borderless
  or Windowed mode. Exclusive fullscreen is out of scope and is documented as a
  requirement rather than detected.

**Closing the board**

- A small close control appears in the board's top-right corner. Left click
  closes it; right click does nothing, there or anywhere else on the board.
  Right click is how a player moves, so a misplaced one must cost nothing.

**Icons and layout**

- Champion portraits, ultimate icons, and summoner-spell icons are fetched from
  Data Dragon into the existing per-patch cache and rendered in the board. The
  fetch happens off the UI thread; rows and slots fall back to text until an
  icon arrives, and permanently if it never does.
- The portrait replaces the champion-name label and the level display is
  removed, taking roughly a third off the board's width. Level is still read and
  still selects the cooldown rank; it is simply not drawn.
- A counting slot darkens its icon and draws the remaining seconds outlined
  over it, so state is readable without depending on window opacity.
- Rows are ordered top, jungle, mid, bot, support, falling back to the live
  client's own order where no position is reported.
- Scale changes icon size, padding, and font together. Today it changes only
  fonts, so "smaller" means smaller text in the same box.

**Countdown consistency**

- The remaining-seconds caption is computed with a true ceiling rather than
  `int(x) + 1`, which currently displays one second more than the real duration
  for the first frame after a press.
- Repaints are scheduled against a fixed grid instead of chained from the end of
  the previous repaint.
- The roster poll moves off the UI thread, so a slow or timing-out Live Client
  request can no longer stall the countdown.

**LTK settings ordering**

- `enforceSkinhackScan` is applied at a point where LTK's `settings.json`
  actually exists, and a skipped application is logged instead of returning
  `False` in silence.

## Capabilities

### New Capabilities

None. Every change below modifies behaviour that is already specified.

### Modified Capabilities

- `cooldown-board`: adds requirements covering overlay presentation (borderless,
  non-activating, drag, persisted position, DPI), not obscuring the game,
  foreground binding, icon rendering, and countdown consistency; amends "Left
  click only" so right click is inert everywhere, and "The board follows the
  match" for the in-board close control.
- `ltk-integration`: amends "The write surface stays minimal" so that applying a
  managed setting is specified against LTK's file lifecycle, not merely against
  the set of keys written.

## Impact

**Code**

- `cooldown/panel.py` — the bulk of the change: window styling, drag, icons,
  repaint scheduling.
- `cooldown/` — one new module for the Win32 overlay and foreground-window
  calls. It stays inside the package boundary; `cooldown/` is already exempt
  from the adapter rule in `tests/test_architecture.py`, so this needs a layer
  assignment but no rule change.
- `cooldown/catalog.py` — icon URL resolution and download into the existing
  cache directory.
- `cooldown/board.py` — the caption ceiling.
- `cooldown/__init__.py` — unchanged; the four public functions gain nothing.
- `settings.py` — `cooldown_left` and `cooldown_top` already exist and are
  currently dead; they start being read and written. `OPACITY_CHOICES` gains a
  lower value and `DEFAULT_OPACITY` drops.
- `app.py`, `tray.py` — untouched by the overlay work.
- `ltk.py`, `app.py` — the settings-ordering fix.

**Dependencies**

None added. Tk 8.6.15 decodes Data Dragon's PNGs natively and `PhotoImage.subsample`
resizes them, so Pillow is not needed for this.

**Risk**

The overlay work is Win32-adjacent and cannot be covered by the existing suite;
it needs a live match to confirm. The icon and timing work is testable.

## Measured versus inferred

Measured on this machine, 2026-08-20:

- `game.cfg:33` reads `WindowMode=0`. League is in exclusive fullscreen, which
  is why the board is invisible in game.
- `overrideredirect(True)` removes the title bar and preserves size; Tk already
  sets `WS_EX_TOPMOST`, `WS_EX_TOOLWINDOW`, and `WS_EX_LAYERED` on the root
  window, and `WS_EX_NOACTIVATE` can be added and coexists with alpha.
- `winfo_id()` returns a child HWND with exstyle `0x4`. The top-level window is
  `GetAncestor(hwnd, GA_ROOT)`. Style changes applied to the wrong handle
  silently do nothing.
- A chained `after(250)` loop runs at a mean interval of 256.8 ms — 2.7 % slow —
  and the displayed digit holds for 1.011 s to 1.044 s instead of 1.000 s.
- Data Dragon portraits are 128 px / ~28.5 KB and spell icons 64 px / ~6.5 KB;
  Tk 8.6.15 decodes both, and `subsample` yields 32 px from either. Individual
  fetches took 0.9 s to 4.6 s.
- `%APPDATA%\dev.leaguetoolkit.manager\settings.json` currently holds
  `firstRunComplete: true` and `enforceSkinhackScan: true`. On first run
  `apply_settings` executes before LTK has ever run, so the file does not exist,
  the guard returns `False`, and the subsequent advertised launch writes LTK's
  defaults.

- LTK accepts and preserves `enforceSkinhackScan: false` when it is written
  BOM-free while LTK is closed, and does not rewrite `settings.json` on exit —
  file mtime is unchanged across a full run and its log records no save. It
  enforces nothing: an innocuous control written at the same time survived too.
  A UTF-8 BOM causes LTK to reject the entire file and replace it with its
  defaults; `atomic.py` encodes without one, so the application is unaffected.
  Therefore the ordering fix is sufficient on its own.

Inferred, not measured:

- That a borderless, topmost, non-activating Tk window will in fact draw over
  League in Borderless mode. This is the premise of the whole change and must be
  confirmed in a live match before the change is applied.

## Rejected alternatives

- **DirectX hooking / injection, as Overwolf does for Porofessor.** It is the
  only thing that works in exclusive fullscreen, and it is an entire product
  rather than a feature. Injecting into a running League process is also
  precisely what anticheat is built to notice. Rejected outright.
- **Leaving the title bar and only fixing the icons and timing.** Cheapest, but
  it does not deliver an overlay, which is the point of the board.
- **Right click to close.** Proposed and then withdrawn: right click is how a
  player moves, so binding destruction to it makes every stray click near the
  board cost something. A close control that occupies a few pixels is cheaper
  than a gesture that cannot be taken back.
- **Making the tray entry a toggle.** Costs no screen space and reuses functions
  that already exist, but closing the board would mean leaving the game to reach
  the tray.
- **A global hotkey.** Works from inside the game, but `RegisterHotKey` needs a
  message pump the Tk loop does not own, and any binding can collide with the
  user's own.
- **Passing right clicks through to the game while the board stays visible.**
  This is what Overwolf gives Porofessor, and Win32 alone cannot do it:
  `WS_EX_TRANSPARENT` passes *all* input through, and colour-key transparency
  passes input through only on pixels that are by definition invisible. A
  visible pixel that right clicks fall through requires subclassing the window
  procedure to answer `WM_NCHITTEST`, which is more machinery than a small
  close control.
- **Detecting exclusive fullscreen from `game.cfg` and warning.** Path
  discovery, parsing, and three tests to say what one line of documentation
  says. Cut.
- **Differential repaint scheduling tied to each timer's next second boundary.**
  More precise than a fixed grid and more machinery than the problem justifies;
  a fixed grid removes the drift, and the residual error is bounded by the
  refresh interval.
