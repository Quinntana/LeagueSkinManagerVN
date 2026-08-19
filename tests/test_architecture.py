"""Architecture enforcement.

Ports and Adapters with a layered import rule. Architecture written in a
comment rots; architecture that fails CI does not.

The rule is one-directional: a module may import from strictly lower layers
only. That is what keeps the domain free of I/O, keeps adapters ignorant of
each other, and keeps every concrete type inside the composition root.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "league_skin_manager"

# Lower number = lower layer. A module may import its own layer or below.
LAYERS: dict[str, int] = {
    # L0 - foundation: no imports from anywhere else in the package.
    # config sits here rather than in the domain because it is pure constants
    # and path arithmetic with no dependencies of its own, and because the
    # platform adapters legitimately need the application's identity for the
    # mutex name, the Run key, and the Start Menu entry.
    "config": 0,
    "atomic": 0,
    "hashing": 0,
    "logging_setup": 0,
    "windows": 0,
    # L1 - domain: values and validation, no I/O of its own
    "settings": 1,
    "fantome": 1,
    # L2 - adapters: one external system each
    "github": 2,
    "cache": 2,
    "seed": 2,
    "ltk": 2,
    "porofessor": 2,
    "process_watch": 2,
    "uninstall": 2,
    # L3 - use cases
    "sync": 3,
    # L4 - user interface
    "tray": 4,
    "cooldown": 4,
    # L5 - composition root
    "app": 5,
    "__main__": 5,
}

# Modules permitted to import third-party or OS-level packages. Everything
# else must reach the outside world through an adapter.
IO_MODULES = frozenset(
    {"windows", "github", "ltk", "cache", "seed", "porofessor", "process_watch", "uninstall"}
)
IO_IMPORTS = frozenset({"requests", "psutil", "ctypes", "winreg", "subprocess", "urllib3"})


def module_files() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def module_name(path: Path) -> str:
    """Return the layer key: the top-level module or subpackage name."""

    relative = path.relative_to(PACKAGE)
    return relative.parts[0] if len(relative.parts) > 1 else relative.stem


def local_imports(path: Path) -> set[str]:
    """Return the sibling modules a file imports, by layer key."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    own = module_name(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("league_skin_manager"):
                    parts = node.module.split(".")
                    if len(parts) > 1:
                        found.add(parts[1])
                continue
            if node.module:
                target = node.module.split(".")[0]
                # A relative import inside a subpackage may name a sibling
                # file; that is intra-package and not a layer crossing.
                found.add(own if node.level == 1 and _is_subpackage(path) else target)
            else:
                for alias in node.names:
                    found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("league_skin_manager"):
                    parts = alias.name.split(".")
                    if len(parts) > 1:
                        found.add(parts[1])
    return {name for name in found if name in LAYERS}


def external_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found & IO_IMPORTS


def _is_subpackage(path: Path) -> bool:
    return len(path.relative_to(PACKAGE).parts) > 1


def test_every_module_is_assigned_a_layer() -> None:
    """A new module must be placed deliberately, not left unclassified."""

    unassigned = {
        module_name(path)
        for path in module_files()
        if module_name(path) not in LAYERS and module_name(path) != "__init__"
    }
    assert not unassigned, f"Modules missing a layer assignment: {sorted(unassigned)}"


@pytest.mark.parametrize("path", module_files(), ids=lambda p: str(p.name))
def test_imports_only_reach_downward(path: Path) -> None:
    """The layering rule: nothing imports from a higher layer."""

    own = module_name(path)
    if own not in LAYERS:
        pytest.skip(f"{own} is not a layered module")
    own_layer = LAYERS[own]
    for imported in local_imports(path):
        if imported == own:
            continue
        assert LAYERS[imported] <= own_layer, (
            f"{path.relative_to(PACKAGE)} (layer {own_layer}) imports "
            f"{imported} (layer {LAYERS[imported]}), which is a higher layer"
        )


@pytest.mark.parametrize("path", module_files(), ids=lambda p: str(p.name))
def test_only_adapters_touch_the_outside_world(path: Path) -> None:
    """Domain and use-case code reaches the outside only through adapters."""

    own = module_name(path)
    if own in IO_MODULES or own == "cooldown":
        return
    offenders = external_imports(path)
    assert not offenders, (
        f"{path.relative_to(PACKAGE)} imports {sorted(offenders)} directly; "
        "route it through an adapter instead"
    )


def test_the_composition_root_is_the_only_place_wiring_everything() -> None:
    """app.py knows concrete types; nothing else should know them all."""

    counts = {
        module_name(path): len(local_imports(path))
        for path in module_files()
        if module_name(path) in LAYERS
    }
    assert counts["app"] == max(counts.values()), (
        "the composition root should have the widest import surface"
    )


def test_the_cooldown_package_exposes_only_its_public_functions() -> None:
    """The isolation boundary: the shell must not reach past these four."""

    from league_skin_manager import cooldown

    assert set(cooldown.__all__) == {"open_panel", "close_panel", "is_open", "apply_display"}


def test_nothing_outside_cooldown_imports_its_internals() -> None:
    internals = {"timer", "panel", "host", "live", "catalog", "roster", "board"}
    for path in module_files():
        if _is_subpackage(path):
            continue
        source = path.read_text(encoding="utf-8")
        for name in internals:
            assert f"from .cooldown.{name}" not in source, (
                f"{path.name} reaches past the cooldown package boundary"
            )


def test_the_cooldown_board_is_wired_to_its_data_sources() -> None:
    """Ported modules must be reachable, not merely present.

    live.py and catalog.py were once fully written, tested, and validated
    against the real APIs while nothing imported them, so the board still
    required durations to be typed in. Every other test passed.
    """

    package = PACKAGE / "cooldown"
    wiring = (package / "__init__.py").read_text(encoding="utf-8")
    for module, symbol in (("live", "LiveClient"), ("catalog", "CooldownCatalog")):
        assert f"from .{module} import" in wiring, f"cooldown/{module}.py is never imported"
        assert symbol in wiring, f"{symbol} is imported but never constructed"

    board = (package / "board.py").read_text(encoding="utf-8")
    assert "roster" in board and "resolve" in board, "the board must join both sources"

    panel = (package / "panel.py").read_text(encoding="utf-8")
    assert "manual_definition" not in panel, "the panel must not fall back to typed durations"


def test_the_domain_layer_stays_free_of_application_imports() -> None:
    """Layer 1 must not know about adapters, use cases, or the UI."""

    for path in module_files():
        own = module_name(path)
        if LAYERS.get(own) != 1:
            continue
        for imported in local_imports(path):
            assert LAYERS[imported] <= 1, f"{own} imports {imported} from a higher layer"
