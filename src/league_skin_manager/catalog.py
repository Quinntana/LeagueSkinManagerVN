"""Read-only searchable view of the managed skin manifest."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .sync_service import ManagedState, ManagedStateError


class CatalogError(RuntimeError):
    """The installed-skin catalog could not be read safely."""


@dataclass(frozen=True, slots=True)
class SkinRecord:
    champion: str
    name: str
    source_path: str
    directory: str
    size: int
    content_sha256: str

    @property
    def search_text(self) -> str:
        normalized = _normalize_search(f"{self.champion} {self.name}")
        return f"{normalized} {normalized.replace(' ', '')}"


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    source_commit: str
    patch: str | None
    skins: tuple[SkinRecord, ...]

    @property
    def champions(self) -> tuple[str, ...]:
        return tuple(sorted({skin.champion for skin in self.skins}, key=str.casefold))

    @property
    def total_bytes(self) -> int:
        return sum(skin.size for skin in self.skins)

    def filtered(self, query: str = "", champion: str | None = None) -> tuple[SkinRecord, ...]:
        normalized_query = _normalize_search(query)
        tokens = tuple(token for token in normalized_query.split() if token)
        if normalized_query and " " not in normalized_query:
            tokens += (normalized_query.replace(" ", ""),)
        wanted_champion = champion.casefold() if champion else None
        return tuple(
            skin
            for skin in self.skins
            if (wanted_champion is None or skin.champion.casefold() == wanted_champion)
            and all(token in skin.search_text for token in tokens)
        )


def load_catalog(path: Path) -> CatalogSnapshot:
    """Load the validated managed-state file without touching installed mods."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        state = ManagedState.from_json(raw)
    except FileNotFoundError:
        return CatalogSnapshot(source_commit="", patch=None, skins=())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ManagedStateError) as exc:
        raise CatalogError("The installed-skin catalog is unreadable") from exc

    skins = tuple(
        sorted(
            (
                SkinRecord(
                    champion=entry.champion,
                    name=entry.name,
                    source_path=entry.source_path,
                    directory=entry.directory,
                    size=entry.size,
                    content_sha256=entry.content_sha256,
                )
                for entry in state.entries
            ),
            key=lambda skin: (skin.champion.casefold(), skin.name.casefold()),
        )
    )
    return CatalogSnapshot(
        source_commit=state.source_commit,
        patch=state.patch,
        skins=skins,
    )


def _normalize_search(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    characters = (
        character if character.isalnum() else " "
        for character in decomposed
        if not unicodedata.combining(character)
    )
    return " ".join("".join(characters).split())


__all__ = [
    "CatalogError",
    "CatalogSnapshot",
    "SkinRecord",
    "load_catalog",
]
