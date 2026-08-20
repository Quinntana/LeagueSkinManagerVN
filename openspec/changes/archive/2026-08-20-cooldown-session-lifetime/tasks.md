## 1. The panel: hide instead of quit

- [x] 1.1 Split hiding into `_hidden_by_user` and `_hidden_for_foreground`, with `hide` withdrawing the window rather than quitting the loop, and the foreground watcher clearing only its own flag. Verify with a test asserting that hiding by hand, then losing and regaining foreground, leaves the board hidden.
- [x] 1.2 Make `show` clear the user flag and re-assert topmost. Verify with a test asserting a hidden-then-shown board is visible and still hidden if the game is not in front.
- [x] 1.3 Skip painting while the board is not visible, keeping the repaint schedule anchored. Verify with a test asserting `_paint` performs no canvas work while hidden and shows true remaining time on the next visible paint.

## 2. The package boundary

- [x] 2.1 Change `open_panel` to show an existing session instead of building a second one, and `close_panel` to hide. Verify with a test asserting a second `open_panel` constructs no new board and returns the same window object.
- [x] 2.2 Add `release_panel` to end the session, and export it. Verify with a test asserting it stops the host thread and that `is_open` is false afterwards.
- [x] 2.3 Update `tests/test_architecture.py` for the five-function surface. Verify the boundary test passes and still forbids reaching past it.

## 3. The composition root

- [x] 3.1 Release the session on the game-stopped transition instead of closing the panel. Verify with a test asserting the release path runs when the watcher reports the game gone.
- [x] 3.2 Delete `_suppressed_for_match` and its branches. Verify with a test asserting that hiding mid-match and re-opening from the tray shows the same board, and that no suppression state remains in `app.py`.
- [x] 3.3 Release the session on application shutdown. Verify with a test asserting shutdown leaves no live session.

## 4. One board, and only one

- [x] 4.1 Report visibility out of the package so the shell can tell "a session exists" from "the board is on screen". Verify with a test asserting the two differ while the board is hidden.
- [x] 4.2 Disable the tray's cooldown entry while the board is visible, alongside the existing no-match rule, and re-enable it once hidden. Verify with tray tests covering visible, hidden, and no match.
- [x] 4.3 Make `open_panel` a no-op when the board is already visible, so no route can build a second board. Verify with a test asserting a redundant open constructs nothing and leaves the existing timers untouched.

## 5. Prove the hazard is gone

- [x] 5.1 Add a test that opens, closes, and re-opens the real package repeatedly and asserts the process survives. **Done 2026-08-20.** `tests/test_cooldown_session.py` drives the real boundary in a subprocess and asserts on the exit status, because a Tcl abort does not raise. Verified against the old design first: three open-close-open cycles printed `Tcl_AsyncDelete: async handler deleted by the wrong thread` and killed the process on the first cycle. The same script now completes all three.
- [ ] 5.2 Run the open/close/open cycle against the built executable with a live game, at least three times. Verify by observation and by the absence of a silent exit in the log.

## 6. Close out

- [x] 6.1 Run all four gates and verify the coverage floor still holds.
- [x] 6.2 Update `README.md`: closing hides the board and keeps its timers; everything is released when the game exits.
- [ ] 6.3 Play one live match: hide and show repeatedly, confirm timers keep counting while hidden, confirm an unavailable ultimate shows its cross, and confirm the board is gone once the game exits.
