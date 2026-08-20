## ADDED Requirements

### Requirement: The board is an overlay, not a window

The board SHALL present as a borderless, always-on-top, non-activating overlay. It SHALL carry no title bar, SHALL NOT take keyboard focus from the game when clicked, SHALL NOT appear in the window switcher, and SHALL remember where the user put it.

#### Scenario: No window chrome
- **THEN** the board SHALL have no title bar and no minimise button
- **AND** it SHALL NOT appear in the alt-tab switcher
- **BECAUSE** the board exists to be small, and chrome costs screen space over a
  running game

#### Scenario: Clicking does not disturb the game
- **WHEN** the user clicks the board
- **THEN** the game SHALL keep keyboard focus
- **AND** the click SHALL still register on the board

#### Scenario: Moving the board
- **WHEN** the user presses the left button anywhere that is not a slot or a
  corner control, and drags
- **THEN** the board SHALL move with the pointer
- **AND** on release its position SHALL be persisted
- **AND** the next launch SHALL restore that position, clamped to a visible
  monitor

#### Scenario: A click is not a drag
- **WHEN** the left button is pressed and released without the pointer moving
  appreciably
- **THEN** the board SHALL NOT move and SHALL persist nothing
- **BECAUSE** otherwise every stray click, and every use of a corner control,
  rewrites the stored position

#### Scenario: Positions survive a scaled display
- **WHEN** the board is placed on a display with a scaling factor other than
  100 %
- **THEN** the restored position SHALL match the placed position
- **BECAUSE** a position saved in logical pixels and restored in physical ones
  drifts further the further it sits from the origin

### Requirement: The board is small before it is faint

The board SHALL be sized down rather than faded out to stay unobtrusive. Every dimension SHALL follow the chosen scale, and opacity SHALL be adjustable from the board itself without leaving the game.

#### Scenario: Scale changes every dimension
- **WHEN** the user chooses a different scale
- **THEN** icon size, padding, and font SHALL all change together
- **AND** the board's overall footprint SHALL change accordingly

#### Scenario: Display settings are reachable without leaving the game
- **THEN** the board SHALL carry a control that cycles opacity in place and a
  control that cycles scale in place
- **AND** both chosen values SHALL be persisted
- **AND** the board SHALL show the current opacity and scale, and nothing else
- **AND** the board SHALL carry no permanent control other than those two and
  the close control

#### Scenario: The range reaches what is actually wanted
- **THEN** the offered opacities SHALL extend low enough to be unobtrusive and
  high enough to be plainly legible

### Requirement: Cooldown state is legible at a glance

A slot that is counting SHALL be distinguishable from a ready slot without reading the number, and the number SHALL stay legible at every offered opacity. Legibility SHALL NOT depend on the window's opacity, which applies uniformly to the whole board and cannot be varied per element.

#### Scenario: Counting versus ready
- **WHEN** a slot is counting
- **THEN** its icon SHALL be visibly darkened relative to a ready slot
- **AND** the remaining seconds SHALL be drawn over it

#### Scenario: The number survives a faint board
- **WHEN** the board is at its lowest opacity
- **THEN** the remaining seconds SHALL be drawn with an outline against the
  icon beneath it
- **BECAUSE** a uniform window alpha fades text and background together, so
  contrast has to come from inside the slot

#### Scenario: The spell stays identifiable while counting
- **WHEN** a slot is counting
- **THEN** its icon SHALL remain visible beneath the darkening
- **BECAUSE** the two summoner slots are distinguished by their icons, not by
  their position alone

### Requirement: Rows are ordered by lane

Enemies SHALL be listed top, jungle, mid, bot, support. Where the live client does not report a position, those enemies SHALL be listed last, and the ordering SHALL fall back to the order the live client returned rather than to anything arbitrary.

#### Scenario: A game that reports positions
- **WHEN** the live client reports a position for every enemy
- **THEN** rows SHALL read top, jungle, mid, bot, support, regardless of the
  order the live client listed them in

#### Scenario: A game that reports no positions
- **WHEN** the live client reports no position for any enemy, as in modes that
  have no lanes
- **THEN** the rows SHALL preserve the order the live client returned
- **AND** the board SHALL NOT reorder rows between polls within one match

#### Scenario: Positions are reported for some enemies only
- **THEN** those with a position SHALL be ordered by lane
- **AND** those without SHALL follow them, in the order the live client returned

### Requirement: The overlay follows the game window

The board SHALL be visible only while the League game window is in the foreground, and SHALL restore itself when it returns. It SHALL NOT remain on screen over unrelated applications.

#### Scenario: Leaving the game
- **WHEN** the user switches away from `League of Legends.exe` while a match is
  running
- **THEN** the board SHALL hide
- **AND** every timer SHALL keep running

#### Scenario: Returning to the game
- **WHEN** `League of Legends.exe` becomes the foreground window again
- **THEN** the board SHALL reappear in its previous position
- **AND** SHALL show the timers' current remaining values

### Requirement: Rows are identified by portrait, not by text

Each row SHALL identify its enemy by champion portrait, and each slot by the icon of the ability or summoner spell it tracks. Champion level SHALL NOT be displayed. Icons SHALL come from the same patch-scoped source as the durations and SHALL be cached across launches. A missing icon SHALL degrade to text.

#### Scenario: Icons are shown
- **WHEN** the enemy roster resolves and icons are available
- **THEN** each row SHALL show the champion's portrait in place of their name
- **AND** each slot SHALL show its ability or summoner-spell icon

#### Scenario: Level is used but not shown
- **THEN** champion level SHALL still be read from the live client and used to
  select the cooldown rank
- **AND** it SHALL NOT occupy space on the board

#### Scenario: Icons are not available yet
- **WHEN** icons have not yet been retrieved, or retrieval failed
- **THEN** rows SHALL show the champion name and slots their letter caption
- **AND** the board SHALL remain fully usable

#### Scenario: Retrieval never blocks the board
- **WHEN** icons are being retrieved
- **THEN** the countdown SHALL continue to update at its normal rate
- **AND** clicks SHALL continue to register

### Requirement: The countdown is consistent

The displayed remaining seconds SHALL be the true ceiling of the remaining duration, and the interval between displayed changes SHALL NOT drift or stall. No network request SHALL be able to delay a repaint.

#### Scenario: The first displayed value is correct
- **WHEN** a slot with an N-second cooldown is started
- **THEN** the first value displayed SHALL be N, never N+1

#### Scenario: The countdown does not drift
- **WHEN** a timer counts down
- **THEN** each displayed second SHALL hold for one second, within the refresh
  interval
- **AND** the schedule SHALL NOT accumulate error over the life of the timer

#### Scenario: A slow data source does not stall the display
- **WHEN** a roster poll is slow or times out
- **THEN** the countdown SHALL continue to update at its normal rate
- **BECAUSE** the poll SHALL NOT run on the thread that repaints

## MODIFIED Requirements

### Requirement: Left click only

A slot SHALL respond to left click alone, cycling idle to counting to cancelled and starting afresh rather than resuming. Right click SHALL have no behaviour anywhere on the board. Left click outside a slot SHALL move the board rather than affect any timer.

#### Scenario: The cycle
- **WHEN** an idle slot is clicked **THEN** its timer starts
- **WHEN** a counting slot is clicked **THEN** it is cancelled
- **WHEN** it is clicked again **THEN** a **fresh** timer starts, not a resume
- **AND** there SHALL be no right-click behaviour

#### Scenario: Clicking off a slot
- **WHEN** the left button is pressed anywhere that is not a slot
- **THEN** no timer SHALL start, cancel, or reset
- **AND** the gesture SHALL be interpreted as a drag

#### Scenario: Right click never destroys anything
- **WHEN** the right button is pressed anywhere on the board, including a
  corner control
- **THEN** nothing SHALL happen — no timer changes, and the board SHALL NOT
  close
- **BECAUSE** right click is how the player moves, and a misplaced one must
  cost nothing

### Requirement: The board follows the match

The board SHALL open with the game process when enabled, SHALL close when the match ends however it was opened, and SHALL default to disabled. It SHALL carry its own close control, reachable without a title bar. Closing discards its timers.

#### Scenario: Opening and closing
- **WHEN** `League of Legends.exe` starts and the toggle is on
- **THEN** the board SHALL open — the toggle defaults **off**
- **WHEN** the match ends
- **THEN** the board SHALL close, however it was opened
- **WHEN** the user closes it by hand mid-match
- **THEN** re-opening SHALL be suppressed **for that match only**

#### Scenario: Closing mid-match
- **WHEN** the board is open during a match
- **THEN** a close control SHALL be present in its top-right corner
- **AND** left-clicking it SHALL close the board, leaving the match and the tray
  running
- **AND** right-clicking it SHALL do nothing

#### Scenario: Re-opening by hand after closing
- **WHEN** the user closes the board mid-match and then re-opens it from the
  tray
- **THEN** the board SHALL open with every timer discarded
- **BECAUSE** timers belong to the board's lifetime, and carrying them across a
  close costs more than it is worth

#### Scenario: Enabling automatic opening during a match
- **WHEN** the user turns the automatic toggle on while a match is already
  running
- **THEN** the board SHALL NOT open by itself
- **AND** the setting SHALL take effect at the next match
- **BECAUSE** the board opens on the transition into a match, and a setting
  should record a preference rather than perform an action

#### Scenario: A new match
- **WHEN** the roster identity changes
- **THEN** every timer SHALL reset
- **BECAUSE** carrying a previous game's countdowns forward is worse than
  showing nothing

### Requirement: Display settings live on the board

Opacity and scale SHALL both be adjustable from the board itself and persisted. They SHALL remain adjustable from the tray as well, so they are reachable when the board is closed. The board SHALL carry no permanent controls beyond opacity, scale, and close.

#### Scenario: Adjusting the board
- **THEN** opacity and scale SHALL each be cycled from a control on the board
- **AND** each change SHALL apply immediately and be persisted
- **BECAUSE** both are judged against what is on screen at the time, which
  cannot be seen from a tray menu

#### Scenario: The tray still works
- **THEN** the tray SHALL continue to offer opacity and scale
- **AND** a change made there SHALL apply to a live board
- **BECAUSE** the board is closed outside a match, and the settings must still
  be reachable

#### Scenario: Nothing else earns a place on the board
- **THEN** the board SHALL carry exactly three controls — opacity, scale, and
  close — and one readout showing the current opacity and scale
- **BECAUSE** it is deliberately small and sits over a running game, where
  every permanent element costs screen space

## RENAMED Requirements

- FROM: `### Requirement: Display settings live in the tray`
- TO: `### Requirement: Display settings live on the board`
