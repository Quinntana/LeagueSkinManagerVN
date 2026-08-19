# Working on this project

Read `project.md` first, then the spec for the area you are changing.

## The rule that matters most

**Anything asserted about LTK's on-disk behaviour must be measured, not
inferred.** During this project four decisions reached by careful reasoning
were disproved by experiment, and two further facts were found that no amount
of reading would have produced:

- `watcherEnabled` turned out to be irrelevant to seeding
- `library.json` turned out to repair itself
- syncing turned out not to need LTK closed
- LTK turned out to bootstrap around pre-seeded archives
- a partial `settings.json` is rejected and silently replaced with defaults
- four upstream packages are malformed and are skipped

If you are about to write "LTK will …", copy the data root aside, try it, and
restore. `scripts/` holds nothing; the procedure is: copy
`%APPDATA%\dev.leaguetoolkit.manager`, mutate, launch LTK, observe its log at
`logs/`, then restore the copy.

## Before changing code

- `tests/test_architecture.py` enforces the layering. A new module needs a
  layer assignment or the suite fails.
- Nothing outside `cooldown/` may import its internals.
- Adapters may import `requests`/`psutil`/`ctypes`/`winreg`; nothing else may.

## Gates

```
ruff format --check src tests build.py
ruff check src tests build.py
mypy src/league_skin_manager
pytest
```

All four must pass. Coverage floor is 80%.

## Validate against real data where it exists

Unit tests are necessary and not sufficient. The checks that caught real bugs
were run against live sources:

- cache filenames are upstream Git blob SHAs, so recomputing them is a free
  1,900-case fixture
- package validation must accept exactly what LTK accepts — compare against
  the archive count in LTK's data root
- the Data Dragon rank rules should be run over all 173 champions

## Making a change

Follow OpenSpec: put a proposal in `changes/<change-id>/proposal.md`, the
delta in `changes/<change-id>/specs/<capability>/spec.md`, and a checklist in
`tasks.md`. When it ships, fold the delta into `specs/` and move the change to
`changes/archive/`.
