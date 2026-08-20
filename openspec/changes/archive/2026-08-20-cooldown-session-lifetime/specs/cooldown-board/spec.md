## MODIFIED Requirements

### Requirement: The board follows the match

The board's lifetime SHALL be the match, not the window. It SHALL be created once per game, SHALL default to disabled, and SHALL be released when the game process exits, however it exits. Showing and hiding the board SHALL NOT create or destroy anything, and timers SHALL continue to run while it is hidden.

#### Scenario: Opening and closing
- **WHEN** `League of Legends.exe` starts and the toggle is on
- **THEN** the board SHALL open — the toggle defaults **off**
- **WHEN** the match ends
- **THEN** the board SHALL be released, however it was opened

#### Scenario: Closing mid-match
- **WHEN** the board is on screen during a match
- **THEN** a close control SHALL be present in its top-right corner
- **AND** left-clicking it SHALL hide the board, leaving the match and the tray
  running
- **AND** right-clicking it SHALL do nothing

#### Scenario: Timers keep running while hidden
- **WHEN** the user hides the board with a timer counting
- **THEN** that timer SHALL keep counting down
- **AND** showing the board again SHALL display its true remaining time
- **BECAUSE** a player hides the board to see the game, not to abandon what
  they were tracking

#### Scenario: Re-opening by hand after closing
- **WHEN** the user hides the board and later re-opens it from the tray
- **THEN** the same board SHALL be shown, with every timer intact
- **AND** nothing SHALL be constructed a second time

#### Scenario: One board per game, and only one
- **WHEN** a board is already on screen
- **THEN** the action that opens it SHALL be unavailable
- **AND** no second board SHALL be created by any route
- **BECAUSE** a game has one set of enemy cooldowns, so a second board could
  only disagree with the first

#### Scenario: Hiding releases the lock
- **WHEN** the user hides the board
- **THEN** the action that opens it SHALL become available again
- **AND** using it SHALL show the existing board rather than build one

#### Scenario: Hiding is not undone by the game regaining focus
- **WHEN** the user hides the board, switches away from the game, and returns
- **THEN** the board SHALL stay hidden
- **BECAUSE** hidden-by-the-user and hidden-because-the-game-is-not-in-front
  are different states, and only the second is automatic

#### Scenario: The session is released with the game
- **WHEN** the game process exits — normally, by crashing, or by losing power
- **THEN** the board, its timers, its roster polling, and its interpreter SHALL
  all be released
- **AND** no timer SHALL survive into a later game

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

### Requirement: Cooldown state is legible at a glance

A slot that is counting SHALL be distinguishable from a ready slot without reading the number, and the number SHALL stay legible at every offered opacity. A slot whose ability is unavailable SHALL be distinguishable from both. Legibility SHALL NOT depend on the window's opacity, which applies uniformly to the whole board and cannot be varied per element.

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

#### Scenario: An unavailable ability is crossed out
- **WHEN** an ability cannot be tracked — an ultimate not yet learned at the
  enemy's level, or one whose cooldown cannot be inferred at all
- **THEN** the slot SHALL be marked with a cross over its icon
- **AND** the icon SHALL remain recognisable beneath it
- **BECAUSE** darkening it far enough to read as unavailable destroys the icon,
  and the icon is what tells the two summoner slots apart

#### Scenario: The spell stays identifiable while counting
- **WHEN** a slot is counting or unavailable
- **THEN** its icon SHALL remain visible
- **BECAUSE** the two summoner slots are distinguished by their icons, not by
  their position alone
