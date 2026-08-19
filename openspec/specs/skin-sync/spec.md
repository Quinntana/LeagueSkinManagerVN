# Skin synchronisation

## Requirement: The upstream commit is the only change signal

#### Scenario: Nothing has changed
- **WHEN** the application starts
- **THEN** it SHALL read the branch head with one request
- **AND** if that equals the last completed sync it SHALL stop
- **AND** it SHALL NOT fetch the tree, stat the cache, or contact LTK

#### Scenario: The commit has moved
- **THEN** one recursive tree request supplies every path, byte count, and
  Git blob SHA — the tree **is** the manifest, so integrity metadata is free

## Requirement: Only base skins are selected

#### Scenario: Filtering the tree
- **WHEN** the tree is read
- **THEN** only `skins/<champion>/<name>.fantome` SHALL be selected
- **BECAUSE** matching on exact depth is more durable than recognising chroma
  naming conventions
- **AND** the whole-repository archive SHALL NOT be fetched: it is 2.4 GB
  against 53 MB of base skins

## Requirement: A truncated source cannot empty the library

#### Scenario: The manifest looks wrong
- **WHEN** a manifest holds fewer than 1,000 assets or fewer than 100
  champions, or any asset is empty, oversized, duplicated, or not a base skin
- **THEN** the sync SHALL abort before touching LTK
- **BECAUSE** wipe-and-reseed is only safe when the replacement set is
  known to be complete

## Requirement: Downloads are verified as they arrive

#### Scenario: A package is fetched
- **THEN** its Git blob SHA SHALL be computed over the incoming bytes
- **AND** a size or digest mismatch SHALL reject it without a second pass
- **AND** the file SHALL be written to a temporary name and renamed on success

#### Scenario: A package is malformed
- **WHEN** a `.fantome` fails ZIP-safety or metadata validation
- **THEN** it SHALL be discarded and counted, not silently skipped
- **AND** the reported skin count SHALL reflect what LTK will actually accept
- **NOTE** the live source has four such packages, all missing `Description`

## Requirement: The cache is content-addressed

#### Scenario: Deciding what to download
- **WHEN** the cache is checked for a package
- **THEN** membership SHALL be a name and size check, never a rehash
- **BECAUSE** the filename **is** the Git blob SHA, and nothing is written
  under a digest name until it has been verified

#### Scenario: The source drops a skin
- **WHEN** a sync completes
- **THEN** cache entries not named by any current asset SHALL be pruned

## Requirement: LTK's library is wiped and reseeded, never patched

#### Scenario: Applying a change
- **WHEN** the commit has moved and the cache is filled
- **THEN** `archives/` and `mods/` SHALL be emptied **completely**, including
  anything imported by hand
- **AND** every cached package SHALL then be copied in
- **BECAUSE** converging from any starting state is worth more than the cost
  of a local file copy, and it removes every ledger the previous design needed

#### Scenario: A package is staged
- **THEN** it SHALL be written under a `.part` name and renamed
- **SO** a partially copied file is never adoptable

## Requirement: An interrupted sync repairs itself

#### Scenario: The application dies mid-sync
- **WHEN** a sync fails or is interrupted at any point
- **THEN** the commit marker SHALL remain unwritten
- **AND** the next sync SHALL repeat the whole operation
- **SO** there is no journal, no rollback, and no partial state to recover

## Requirement: State is not a ledger

#### Scenario: What is persisted
- **THEN** the application SHALL record only the commit, patch label, skin
  count, and timestamp
- **AND** it SHALL NOT record a per-skin manifest or digest index
- **BECAUSE** a recorded mapping between "what we installed" and "what is on
  disk" can disagree with reality; the previous design's 1.9 MB of such
  bookkeeping is where its corruption-recovery machinery came from
