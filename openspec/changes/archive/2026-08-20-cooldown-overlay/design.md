# Design

## Context

See `proposal.md` — Why. Requirements are in `specs/`.

Constraints that shape everything below:

- The board owns its own Tk root on its own non-daemon thread. Tk requires the
  interpreter to be created and driven by one thread, so anything that blocks
  that thread is visible as a stalled countdown.
- `cooldown/` is the one enforced internal boundary. It is already exempt from
  the adapter rule in `tests/test_architecture.py`, so `ctypes` may be used
  inside it, but nothing outside may reach past its four public functions.
- No new runtime dependency. Tk 8.6.15 decodes Data Dragon's PNGs and
  `PhotoImage.subsample` resizes them (measured).

## Goals / Non-Goals

**Goals**

- One Win32 surface inside `cooldown/`, not ctypes scattered across the panel.
- The repaint thread never performs I/O.
- The change is reversible: overlay styling is additive to the existing window.

**Non-Goals**

- Click-through. The board exists to be clicked.
- Rendering into League's swap chain by any means.
- Making the panel resizable, themable, or configurable beyond the two existing
  tray presets.

## Decisions

### D1 — A single `cooldown/overlay.py` for all Win32 calls

Window styling and foreground detection both need `user32`. Putting them in one
module keeps `panel.py` about layout, and gives the Win32 code a seam that can
be substituted in tests the way `LOL_Minimap_Tracker` does with injected
`user32` doubles.

It is assigned to the same layer as the rest of `cooldown/`; no change to the
layer table is needed, only an entry so `test_architecture.py` does not fail the
unassigned-module check.

*Alternative rejected:* extending the existing top-level `windows.py` (L0). The
foreground-window watcher is only meaningful to the board, and `windows.py` is
imported by the whole application — widening it for one consumer inverts the
boundary the package exists to hold.

### D2 — Style the `GA_ROOT` handle, not `winfo_id()`

Measured: `winfo_id()` returns a child HWND whose exstyle is `0x4`. The
top-level carries `WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED` and is
reached through `GetAncestor(hwnd, GA_ROOT)`. Applying `WS_EX_NOACTIVATE` to the
child silently does nothing.

The overlay module therefore resolves the root handle once, applies the style,
reads it back, and reports whether it took — the same verify-after-write shape
`clickthrough.py` uses in the neighbouring project.

### D3 — Foreground binding by polling, on the existing repaint tick

`GetForegroundWindow` → `GetWindowThreadProcessId` → `QueryFullProcessImageNameW`
is a cheap synchronous triple with no allocation beyond one buffer. Running it
on the repaint tick avoids a second thread and a second piece of lifecycle.

*Alternative rejected:* `SetWinEventHook` for `EVENT_SYSTEM_FOREGROUND`. Event
driven and exact, but it requires a message pump owned by the hooking thread,
which Tk's loop is not, and it adds a callback that must survive being called
during teardown. Polling is a comparison per tick.

### D4 — Fixed-grid repaint scheduling

Measured: chaining `after(250)` from the end of the previous repaint runs at a
mean 256.8 ms — 2.7 % slow, accumulating — and digits hold 1.011–1.044 s.

The fix is to keep a monotonic anchor taken when the loop starts and schedule
each tick as the delay to the next multiple of the interval from that anchor,
clamped to at least 1 ms. Work done during a tick is then absorbed by the next
delay instead of pushing the whole schedule forward.

Residual error stays bounded by the interval rather than growing, which is what
the spec requires. Timer values themselves are already read from a monotonic
clock and were never wrong — only the sampling grid was.

*Alternative rejected:* scheduling each repaint at the earliest timer's next
second boundary. More precise, and it makes the tick rate depend on board state,
which is more machinery than the residual error justifies.

### D5 — Off-thread roster polling with a published snapshot

`CooldownBoard.refresh()` currently performs a blocking HTTPS request with a
1.0 s timeout on the Tk thread every 5 s. It moves to a small worker thread that
writes its result under the board's existing lock; `rows()` reads whatever was
last published. The board already holds a lock and already tolerates a failed
poll, so this is a relocation rather than a new concurrency model.

### D6 — Icons fetched by the same worker, handed over as file paths

`catalog.py` gains icon-URL resolution and download into the existing per-patch
cache directory; `CooldownDefinition.icon_path` and
`EnemyCooldownLoadout.champion_icon_path` — both already declared and always
`None` — start being populated.

Measured fetch latency was 0.9–4.6 s per icon, so this must not touch the Tk
thread. Icons are resolved when a roster resolves, on the worker, and the panel
constructs `PhotoImage` objects lazily on the Tk thread from paths that already
exist on disk.

**`PhotoImage` references must be held.** Tk garbage-collects an image whose
last Python reference drops, leaving a blank square. The panel keeps a dict
keyed by path for the life of the window.

Sizing uses integer `subsample` only: portraits are 128 px and spell icons
64 px, so ÷4 and ÷2 both yield 32 px against a 34 px cell. Other scale presets
pick a different integer factor rather than interpolating.

### D7a — Positions are stored in one coordinate space

Measured on the development machine: the primary display reports 1536x864 to a
non-DPI-aware process and is physically 1920x1080 — 125 % scaling. A position
saved by a process in one awareness mode and restored by a process in the other
lands in the wrong place, and the error scales with the distance from the
origin.

The panel therefore declares per-monitor DPI awareness at start, as
`LOL_Minimap_Tracker.integrations.league_window.enable_process_dpi_awareness`
does, so saved and restored coordinates are always physical pixels. Clamping on
restore covers the remaining case of a monitor that has since been removed or
rearranged.

### D7 — Fullscreen detection is advisory and best-effort

`WindowMode` is read from League's `game.cfg`. `0` is exclusive fullscreen.
The file is found relative to the running game's image path; if it cannot be
located or parsed, no warning is issued and the board opens normally. A warning
must never be able to prevent the board from opening.

### D8 — A close control in the corner; right click stays inert

A small close control in the top-right, bound to `<Button-1>` only, calling the
same path `WM_DELETE_WINDOW` used to — so `on_closed` still fires and
suppress-for-this-match keeps working unchanged.

Right click is bound to nothing, anywhere on the board, including the close
control itself. Right click is how a player moves; a gesture the user makes
constantly for another reason must not destroy anything.

Measured 2026-08-20: in one Practice Tool session with the mockup, right click
landed on the board **13 times**. Whether those were deliberate probes or real
misclicks is not established, but the order of magnitude settles the question —
had right click been bound to close, the board would not have survived the
session.

*Alternative rejected:* passing right clicks through to the game while the board
remains visible. Win32 offers `WS_EX_TRANSPARENT`, which passes **all** input
through, and colour-key transparency, which passes input through only on pixels
of the exact key colour — pixels that are by definition invisible. No
combination yields a visible pixel that a right click falls through, short of
subclassing the window procedure to answer `WM_NCHITTEST`. Porofessor gets this
from Overwolf's input layer, not from Win32. The residual cost is that right
clicks landing on the board are swallowed; it is mitigated by footprint and
placement, not by code.

### D9 — Opacity, not transparency, keeps the game visible

The complaint is that the board conceals what is behind it. The existing
window-wide alpha already addresses that; the default was simply too high.

`DEFAULT_OPACITY` drops and `OPACITY_CHOICES` extends downward. No new
mechanism: the tray already applies opacity to a live board, and the value is
already persisted.

Measured 2026-08-20: offered `0.35 / 0.45 / 0.55 / 0.70 / 0.85` in a live game,
the user cycled the full range three times and settled on **0.35** on both
occasions they stopped. The shipped default is `0.85` and the lowest existing
preset is `0.55`, so the whole usable range currently sits above what was
actually wanted.

*Alternative rejected:* colour-keying the background so only the cells are
drawn. It removes the box entirely, and with it the sense of the board as one
object; it also introduces a failure mode where any pixel of a champion portrait
matching the key colour becomes a hole. Window alpha has neither problem.

The trade-off to accept: alpha applies to the whole window, so captions fade
with the background. If the countdown is not legible at the new default, the
answer is a smaller board at higher opacity rather than a large faint one, which
the existing scale presets already provide.

### D9a — Slots are Canvases, and state is carried inside the slot

Window alpha is uniform: it cannot fade the background while leaving the number
solid. Contrast therefore has to come from within each slot, which a `Label`
cannot express.

Each slot becomes a `tk.Canvas` of icon size, painted per repaint:

| State | Painting |
|---|---|
| idle | icon at full brightness, neutral border |
| counting | icon, then a black rectangle with `stipple="gray50"` over it, then the remaining seconds in white with a black outline drawn as eight one-pixel offsets |
| ready | icon, `up` in the ready colour, ready-coloured border |

`stipple` is Tk's dither pattern — a 50 % checkerboard — which is how it fakes
per-pixel transparency without a compositor or an imaging library. It is the
reason this needs no new dependency.

Chosen 2026-08-20 by comparison in a live game against three alternatives:
leaving the icon lit (the number was lost against bright art), darkening without
an outline (better, still marginal at low opacity), and hiding the icon behind a
flat tile (most readable, but it throws away which summoner spell the slot is —
position distinguishes `D` from `F`, not Flash from Ignite).

### D9b — Rows are portraits, and level is read but not drawn

A champion portrait replaces the `width=9` name label, which is roughly a third
of the board's width. The portrait costs nothing extra: the same Data Dragon
request pattern and cache already serve the spell icons.

Level stays in `RosterMember` and is still what selects the cooldown rank. It
simply stops being rendered — the player can read levels from the scoreboard,
and the board's whole purpose is to be small.

### D9c — Both display settings live on the board, and still in the tray

Opacity and scale are both judged against what is on screen at the time, which
is precisely what a tray menu cannot show. Both therefore get a cycling control
on the board, alongside a readout of their current values.

The tray keeps its entries: the board does not exist outside a match, and the
settings have to be reachable when it is closed. The tray and the board write
the same two persisted values.

This reverses the original "Display settings live in the tray" requirement,
which was written before there was anything to look at. It is captured as a
`MODIFIED` requirement rather than left as a silent contradiction.

The cost is three controls and a readout on a surface whose whole point is being
small. That is accepted deliberately, on the understanding that the smallest
scale preset must still fit them.

### D10 — A drag must be distinguished from a click

Measured 2026-08-20: in the mockup, every `<ButtonRelease-1>` recorded a drag,
including releases where the pointer had not moved and releases that followed a
click on a corner control — because release propagates up the bindtags to the
containers that carry the drag handler.

Left unfixed, that writes a position to `settings.json` on every stray click on
the board, and on every use of the close and opacity controls.

The drag handler therefore records the press position and treats the gesture as
a drag only once the pointer has moved beyond a small threshold. A release below
that threshold persists nothing.

## Risks / Trade-offs

- **The premise may be wrong** — a borderless topmost Tk window might still not
  draw over League in Borderless mode → confirm in a live match *before* writing
  the panel changes. This is T1 in `tasks.md` and gates the rest.
- **`WS_EX_NOACTIVATE` can make a window unclickable in some compositing
  situations** → the overlay module reads the style back and reports failure;
  if clicks stop registering, the flag is dropped and the board keeps focus
  behaviour it has today.
- **Removing the title bar before the tray toggle exists strands the user with
  no close control** → the toggle ships in the same change and is listed before
  the styling work.
- **Position persistence can restore off-screen** after a monitor change →
  clamp to a visible monitor on restore, as the neighbouring project does.
- ~~**LTK may rewrite `settings.json` on exit**, clobbering the managed value~~ →
  measured 2026-08-20: it does not, and it enforces nothing. The ordering fix
  stands alone. See `tasks.md` 1.2.
- **A UTF-8 BOM makes LTK discard the whole settings file** and restore its
  defaults → `atomic.py` encodes without one and must keep doing so; worth a
  test that asserts the written bytes do not start with `EF BB BF`.
- **Icon fetches add ~277 KB and up to 20 requests per new patch** → cached per
  patch alongside the existing metadata, so this is once per patch, not per
  match.

## Migration Plan

No data migration. `cooldown_left` / `cooldown_top` already exist in the
settings schema and are currently unread, so an existing settings file needs no
version bump and an older build ignores what a newer one writes.

Rollback is reverting the change; nothing persisted by it is required by
anything else.

## Open Questions

- Which integer `subsample` factor pairs with each scale preset. Cosmetic,
  decided while implementing, and it changes neither the specs nor the tasks.
- Whether the fullscreen warning should fire once per match or once per
  application run. Once per match is specified; if it proves noisy that is a
  one-line change with no structural effect.
