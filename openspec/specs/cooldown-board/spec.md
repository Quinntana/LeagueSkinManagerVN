# Enemy cooldown board

## Purpose

Show enemy ultimate and summoner-spell timers during a match, sourcing every
duration from the game and Data Dragon rather than from the user. Isolated so
that its failure cannot reach the skin pipeline, and vice versa.

An optional overlay, off by default, isolated behind four functions.

## Requirements

### Requirement: Durations come from the game, not from the user

The board SHALL source enemy identities from Riot's Live Client Data API and base cooldowns from Data Dragon. A duration SHALL NOT be entered by the user.

#### Scenario: A match is in progress
- **WHEN** the board polls
- **THEN** the enemy roster SHALL come from Riot's Live Client Data API at
  `127.0.0.1:2999`, supplying champion, level, and summoner spells
- **AND** base cooldowns SHALL come from Data Dragon, cached per realm version
- **AND** no duration SHALL ever be typed by the user

#### Scenario: No match is running
- **THEN** the roster SHALL report *unavailable*, which is a normal state and
  not an error

#### Scenario: Player identity
- **THEN** participants SHALL be keyed by a one-way hash
- **SO** no Riot ID reaches memory or a log

### Requirement: Ranks are not assumed

A duration SHALL be derived only from a rank layout that can be inferred from champion level. Any other layout, and any charge, resource, toggle, or repeat-cast ability, SHALL render disabled with a reason.

Ultimates are **not** uniformly three ranks. A naive 6/11/16 mapping is wrong
for a real set of champions.

#### Scenario: Resolving a duration
- **WHEN** an ultimate has three ranks
- **THEN** levels 6/11/16 select ranks 1/2/3
- **WHEN** it has one rank (Jayce) or four (Elise, Karma, Nidalee)
- **THEN** the documented level mapping applies
- **WHEN** it has any other rank count
- **THEN** it SHALL be marked unsupported

#### Scenario: Charge-based abilities
- **WHEN** the champion's ultimate uses charge, resource, toggle, or
  repeat-cast behaviour, or the spell is Smite
- **THEN** it SHALL render disabled with a reason
- **BECAUSE** a flat base cooldown would be actively misleading
- **NOTE** at patch 16.16.1 this resolves 162 of 173 ultimates

### Requirement: Left click only

A slot SHALL respond to left click alone, cycling idle to counting to cancelled and starting afresh rather than resuming. Right click SHALL have no behaviour.

#### Scenario: The cycle
- **WHEN** an idle slot is clicked **THEN** its timer starts
- **WHEN** a counting slot is clicked **THEN** it is cancelled
- **WHEN** it is clicked again **THEN** a **fresh** timer starts, not a resume
- **AND** there SHALL be no right-click behaviour

### Requirement: The board follows the match

The board SHALL open with the game process when enabled, SHALL close when the match ends however it was opened, and SHALL default to disabled.

#### Scenario: Opening and closing
- **WHEN** `League of Legends.exe` starts and the toggle is on
- **THEN** the board SHALL open — the toggle defaults **off**
- **WHEN** the match ends
- **THEN** the board SHALL close, however it was opened
- **WHEN** the user closes it by hand mid-match
- **THEN** re-opening SHALL be suppressed **for that match only**

#### Scenario: A new match
- **WHEN** the roster identity changes
- **THEN** every timer SHALL reset
- **BECAUSE** carrying a previous game's countdowns forward is worse than
  showing nothing

### Requirement: Failure here cannot reach the skin pipeline

A failure in this package SHALL NOT affect skin synchronisation, and a synchronisation failure SHALL NOT affect the board. The package SHALL expose exactly four functions.

#### Scenario: A data source fails
- **WHEN** the live client or Data Dragon is unavailable or raises
- **THEN** the board SHALL degrade to disabled slots and keep running
- **AND** skin synchronisation SHALL be entirely unaffected

#### Scenario: The boundary
- **THEN** the package SHALL expose only `open_panel`, `close_panel`,
  `is_open`, and `apply_display`
- **AND** it SHALL run on its own thread with its own Tk root

### Requirement: Display settings live in the tray

Opacity and scale SHALL be chosen from the tray, applied to a live panel, and persisted. The board itself SHALL NOT carry permanent controls.

#### Scenario: Adjusting the board
- **THEN** opacity and scale SHALL be chosen from the tray, applied live, and
  persisted
- **BECAUSE** the board is deliberately small and sits over a running game,
  where permanent controls would cost screen space
