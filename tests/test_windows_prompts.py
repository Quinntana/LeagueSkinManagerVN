from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from league_skin_manager.windows_prompts import prompt_for_ltk_migration_source


def test_injected_prompt_returns_confirmed_selection(tmp_path: Path) -> None:
    initial = tmp_path / "cslol-manager" / "installed"
    selected = tmp_path / "legacy-manager"
    selector_calls: list[Path] = []
    confirmation_calls: list[Path] = []

    def select(path: Path) -> Path:
        selector_calls.append(path)
        return selected

    def confirm(path: Path) -> bool:
        confirmation_calls.append(path)
        return True

    result = prompt_for_ltk_migration_source(
        initial,
        selector=select,
        confirmer=confirm,
    )

    assert result == selected
    assert selector_calls == [initial]
    assert confirmation_calls == [selected]


def test_injected_prompt_returns_none_when_selection_is_cancelled(tmp_path: Path) -> None:
    confirmation_calls: list[Path] = []

    def confirm(path: Path) -> bool:
        confirmation_calls.append(path)
        return True

    result = prompt_for_ltk_migration_source(
        tmp_path,
        selector=lambda _path: None,
        confirmer=confirm,
    )

    assert result is None
    assert confirmation_calls == []


def test_injected_prompt_returns_none_when_confirmation_is_declined(tmp_path: Path) -> None:
    selected = tmp_path / "legacy-manager"

    assert (
        prompt_for_ltk_migration_source(
            tmp_path,
            selector=lambda _path: selected,
            confirmer=lambda path: path != selected,
        )
        is None
    )


@pytest.mark.parametrize(
    ("selector", "confirmer"),
    (
        (lambda path: path, None),
        (None, lambda _path: True),
    ),
)
def test_injected_prompt_requires_both_dependencies(
    tmp_path: Path,
    selector: object,
    confirmer: object,
) -> None:
    with pytest.raises(ValueError, match="provided together"):
        prompt_for_ltk_migration_source(
            tmp_path,
            selector=selector,  # type: ignore[arg-type]
            confirmer=confirmer,  # type: ignore[arg-type]
        )


class Root:
    def __init__(self) -> None:
        self.withdraw_calls = 0
        self.attribute_calls: list[tuple[Any, ...]] = []
        self.destroy_calls = 0

    def withdraw(self) -> None:
        self.withdraw_calls += 1

    def attributes(self, *args: Any) -> None:
        self.attribute_calls.append(args)

    def destroy(self) -> None:
        self.destroy_calls += 1


def install_fake_tk(
    monkeypatch: Any,
    *,
    directory_result: str,
    confirmation_result: bool = True,
) -> tuple[Root, dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]]]:
    root = Root()
    calls: dict[str, list[tuple[tuple[Any, ...], dict[str, Any]]]] = {
        "directory": [],
        "confirmation": [],
    }

    def askdirectory(*args: Any, **kwargs: Any) -> str:
        calls["directory"].append((args, kwargs))
        return directory_result

    def askyesno(*args: Any, **kwargs: Any) -> bool:
        calls["confirmation"].append((args, kwargs))
        return confirmation_result

    tkinter = ModuleType("tkinter")
    tkinter.Tk = lambda: root  # type: ignore[attr-defined]
    tkinter.filedialog = SimpleNamespace(askdirectory=askdirectory)  # type: ignore[attr-defined]
    tkinter.messagebox = SimpleNamespace(NO="no", askyesno=askyesno)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    return root, calls


def test_default_prompt_uses_safe_confirmation_and_destroys_root(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    initial = tmp_path / "cslol-manager" / "installed"
    selected = tmp_path / "legacy-manager"
    root, calls = install_fake_tk(
        monkeypatch,
        directory_result=str(selected),
    )

    assert prompt_for_ltk_migration_source(initial) == selected

    assert root.withdraw_calls == 1
    assert root.attribute_calls == [("-topmost", True)]
    assert root.destroy_calls == 1
    directory_args, directory_kwargs = calls["directory"][0]
    assert directory_args == ()
    assert directory_kwargs["parent"] is root
    assert directory_kwargs["initialdir"] == str(initial)
    assert directory_kwargs["mustexist"] is True
    confirmation_args, confirmation_kwargs = calls["confirmation"][0]
    assert confirmation_args[0] == "Port CSLOL skins to LTK now"
    assert str(selected) in confirmation_args[1]
    assert confirmation_kwargs == {"default": "no", "parent": root}


def test_default_prompt_cancellation_skips_confirmation_and_destroys_root(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    root, calls = install_fake_tk(monkeypatch, directory_result="")

    assert prompt_for_ltk_migration_source(tmp_path) is None

    assert len(calls["directory"]) == 1
    assert calls["confirmation"] == []
    assert root.destroy_calls == 1
