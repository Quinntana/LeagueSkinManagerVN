## 1. Measure before building

- [x] 1.1 Confirm the premise. **Done 2026-08-20, passed.** A borderless topmost Tk window drew over League in Practice Tool. League's mode was confirmed from its own window, not from `game.cfg`: `WS_CAPTION=False` and the frame covers the full screen, so Borderless, not Windowed. All four extended styles took on the `GA_ROOT` handle (`0x08080088`); left clicks registered despite `WS_EX_NOACTIVATE`; drag and left-click-to-close both worked; and `League of Legends.exe` appeared in the foreground poll, proving the mechanism behind task 5.1 on real data.
- [x] 1.2 Settle whether LTK rewrites `settings.json` on exit. **Done 2026-08-20.** With a BOM-free write while LTK is closed, LTK loads and preserves `enforceSkinhackScan: false`; an innocuous control (`theme`) survives alongside it, so LTK enforces nothing. LTK does not rewrite the file on exit — mtime unchanged across a full run, no `Saved settings` line in its log. A UTF-8 BOM makes LTK reject the whole file (`Failed to parse settings file: expected value at line 1 column 1`) and overwrite it with its defaults; `atomic.py` writes BOM-free, so the application is unaffected.
- [x] 1.3 Settle the visual design against a live game. **Done 2026-08-20.** Three mockups over Practice Tool established: portraits replace name text and cut roughly a third off the width; `dim+ink` (icon darkened with a `gray50` Canvas stipple, remaining seconds drawn with a black outline) beat plain, dim, and flat-tile; the opacity range must extend below the current `0.55` floor; scale must move icon, padding, and font together, since `tk scaling` moves fonts only; and a release with no pointer movement must not count as a drag.

## 2. Timing and ordering — no UI involved

- [x] 2.1 Replace `int(remaining) + 1` with a true ceiling in `board.py`. Verify with a test asserting an N-second cooldown displays `N` on its first frame, never `N+1`.
- [x] 2.2 Order rows by lane in `board.py` using a stable sort over `Role`, with unpositioned enemies last in live-client order. Verify with tests covering all positions reported, none reported, and a mix — the middle case asserting the live client's own order is preserved.
- [x] 2.3 Move the roster poll off the Tk thread onto a worker that publishes under the board's existing lock. Verify with a test asserting `rows()` returns promptly while a poll that sleeps past the refresh interval is in flight.

## 3. Icons

- [x] 3.1 Resolve champion, ultimate, and summoner icon URLs in `catalog.py` and populate `icon_path` / `champion_icon_path`. Verify with a test asserting the fields are non-`None` for a champion with a static ultimate.
- [x] 3.2 Download icons into the per-patch cache on the worker thread, checking the PNG signature before writing. Verify with tests covering a cache hit, a cache miss, a non-PNG response, and a failed request.
- [x] 3.3 Run icon resolution over all 173 champions at the live patch. **Done 2026-08-20, patch 16.16.1.** 173/173 champion portraits, 173/173 ultimate icons, 173/173 summoner icons — no icon failures at all. Usable ultimates 162/173, matching the established baseline: 9 charge/resource/toggle/repeat-cast, 2 dynamic or unavailable base cooldown. 347 distinct PNGs, 5.67 MB, fetched sequentially in 115s; a five-enemy match needs at most 20 of them, off the interface thread and cached per patch.

## 4. The overlay

- [x] 4.1 Add `cooldown/overlay.py` with an injectable `user32`: resolve `GA_ROOT` from a child HWND, apply and verify `WS_EX_NOACTIVATE`, declare per-monitor DPI awareness, and report the foreground process image name. Verify with unit tests using a `user32` double, asserting the root handle is used and a failed style write is reported.
- [x] 4.2 Assign the new module in `tests/test_architecture.py` and verify the suite passes the unassigned-module check.
- [x] 4.3 Apply `overrideredirect` and the non-activating style to the panel. Verify by opening the board and observing no title bar and no alt-tab entry.
- [x] 4.4 Rebuild the panel rows as portrait plus three Canvas slots, dropping the level display and the name text. Verify with a test asserting a row with a portrait path renders no champion name, and one without renders the name.
- [x] 4.5 Implement the `dim+ink` slot painting: icon, `gray50` stipple while counting, remaining seconds outlined in black over white. Verify with a test asserting a counting slot draws the stipple and the outline, and a ready slot draws neither.
- [x] 4.6 Make scale rebuild icon size, padding, and font together, choosing the integer `subsample` factor per preset. Verify with a test asserting the rendered icon size changes between two presets.
- [x] 4.7 Add the three corner controls — opacity cycle, scale cycle, and close, left click only, right click inert everywhere — plus a readout of the current opacity and scale. Verify with tests asserting right click changes nothing anywhere on the board, that each cycle persists its value, and that the close path still fires `on_closed`.
- [x] 4.8 Implement drag with a movement threshold, clamped to a visible monitor, persisting to `cooldown_left` / `cooldown_top`; restore clamped on open. Verify with tests asserting a press-release without movement persists nothing, and an off-screen stored position is clamped back onto a monitor.
- [x] 4.9 Extend `OPACITY_CHOICES` downward and lower `DEFAULT_OPACITY`. Verify the existing settings round-trip and snap-to-nearest tests still pass with the new values.
- [x] 4.10 Move repaint scheduling onto a fixed monotonic grid anchored at loop start. Verify with a test that drives the scheduler against a fake clock and asserts no cumulative drift over 40 ticks.

## 5. Foreground binding

- [x] 5.1 Hide the board when `League of Legends.exe` is not foreground and restore it when it returns, checked on the repaint tick. Verify with a test driving a fake foreground source and asserting visibility follows it while timers keep running.

## 6. LTK settings ordering

- [x] 6.1 Apply the managed setting at a point where LTK's settings file exists, and log every skipped application with its reason. Verify with tests covering a missing settings file, a file without `firstRunComplete`, LTK running, and the success path — each asserting the log records what happened.
- [x] 6.2 Add a test asserting `atomic_write_json` emits no UTF-8 BOM, since a BOM makes LTK discard the file and restore its defaults.
- [x] 6.3 Confirm end to end on a real first run. **Done 2026-08-20, passed.** Reproduced the first-run path by removing LTK's `settings.json` and clearing the sync marker. Observed, in order: `Deferred LTK settings: settings.json has not been written yet` -> sync (0 downloaded, 1931 seeded from cache, 4 rejected) -> `Opening LTK Manager once` -> LTK recreated the file with its own `enforceSkinhackScan: true` -> three `Deferred LTK settings: LTK is running` polls -> LTK closed -> `Applied LTK settings: enforceSkinhackScan` at 13:08:00, 101 seconds after launch. Final file: `enforceSkinhackScan` false, `firstRunComplete` true, `watcherEnabled` untouched, `leaguePath` preserved, all 35 keys intact, no BOM. Under v4.0.0 this setting stayed true through the entire first run.

## 7. Close out

- [x] 7.1 Run all four gates — `ruff format --check`, `ruff check`, `mypy --strict`, `pytest` — and verify the coverage floor of 80% still holds.
- [ ] 7.2 Play one live match in Borderless: board opens automatically, rows ordered by lane, portraits and spell icons present, counting slots dimmed with readable outlined numbers, countdown even, hides on alt-tab, restores on return, drags, opacity cycles from the board, closes from its corner control, and closes when the match ends. Verify by observation against each scenario in `specs/cooldown-board/spec.md`.
- [x] 7.3 Update `README.md` and the release notes: Borderless or Windowed is required, exclusive fullscreen is not supported. Verify the statement is present.
