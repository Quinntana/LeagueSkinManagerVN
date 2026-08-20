"""Tests for Data Dragon icon retrieval.

Icons are decoration: every failure path must end in ``None`` and a text
caption, never in an exception reaching roster resolution. The board has to
keep working on a patch day when the CDN is slow, on a machine with no disk
space, and against a proxy that returns an HTML error page with a PNG name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from league_skin_manager.cooldown.catalog import PNG_SIGNATURE, CooldownCatalog
from league_skin_manager.cooldown.roster import RosterMember, SummonerSpellRef

VERSION = "16.16.1"
PNG = PNG_SIGNATURE + b"\x00\x00\x00\rIHDR" + b"\x00" * 32

CHAMPIONS = {
    "data": {
        "Ahri": {
            "id": "Ahri",
            "name": "Ahri",
            "image": {"full": "Ahri.png"},
            "spells": [
                {"id": "AhriQ"},
                {"id": "AhriW"},
                {"id": "AhriE"},
                {
                    "id": "AhriR",
                    "name": "Spirit Rush",
                    "maxrank": 3,
                    "cooldown": [130.0, 105.0, 80.0],
                    "image": {"full": "AhriR.png"},
                },
            ],
        },
        "Naked": {
            "id": "Naked",
            "name": "Naked",
            "spells": [
                {"id": "a"},
                {"id": "b"},
                {"id": "c"},
                {"id": "NakedR", "name": "R", "maxrank": 3, "cooldown": [1.0, 2.0, 3.0]},
            ],
        },
    }
}

SUMMONERS = {
    "data": {
        "SummonerFlash": {
            "id": "SummonerFlash",
            "name": "Flash",
            "maxrank": 1,
            "cooldown": [300.0],
            "image": {"full": "SummonerFlash.png"},
        }
    }
}


class Response:
    def __init__(self, content: bytes, *, status_error: Exception | None = None) -> None:
        self.content = content
        self._error = status_error

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error


class Session:
    """Serves metadata as JSON and icons as bytes, counting every request."""

    def __init__(self, *, icon: bytes | None = PNG, fail: bool = False) -> None:
        self.icon = icon
        self.fail = fail
        self.icon_calls: list[str] = []

    def get(self, url: str, **_kwargs: Any) -> Any:
        if url.endswith("versions.json"):
            return Response(json.dumps([VERSION]).encode())
        if url.endswith("championFull.json"):
            return Response(json.dumps(CHAMPIONS).encode())
        if url.endswith("summoner.json"):
            return Response(json.dumps(SUMMONERS).encode())
        if "/img/" in url:
            self.icon_calls.append(url)
            if self.fail:
                raise OSError("the CDN went away")
            return Response(self.icon or b"")
        raise AssertionError(f"unexpected url {url}")

    def close(self) -> None:
        return None


def resolve(tmp_path: Path, session: Session | None = None) -> tuple[Any, Session]:
    session = session or Session()
    catalog = CooldownCatalog(tmp_path, session=session)
    member = RosterMember(
        champion_name="Ahri",
        participant_id="p1",
        champion_id="Ahri",
        level=11,
        summoner_spells=(SummonerSpellRef("SummonerFlash", "Flash"),),
    )
    return catalog.loadouts([member])[0], session


# --- the happy path --------------------------------------------------------


def test_a_portrait_is_downloaded_and_cached(tmp_path: Path) -> None:
    loadout, _session = resolve(tmp_path)
    portrait = loadout.champion_icon_path
    assert portrait is not None
    assert portrait.read_bytes() == PNG
    assert portrait.name == "Ahri.png"


def test_the_ultimate_and_summoner_icons_are_resolved(tmp_path: Path) -> None:
    loadout, _session = resolve(tmp_path)
    assert loadout.ultimate.icon_path is not None
    assert loadout.ultimate.icon_path.name == "AhriR.png"
    assert loadout.summoner_spells[0].icon_path is not None
    assert loadout.summoner_spells[0].icon_path.name == "SummonerFlash.png"


def test_icons_are_scoped_to_the_patch(tmp_path: Path) -> None:
    """A new patch must not serve last patch's art."""

    loadout, _session = resolve(tmp_path)
    assert VERSION in loadout.champion_icon_path.parts
    assert "champion" in loadout.champion_icon_path.parts
    assert "spell" in loadout.ultimate.icon_path.parts


def test_a_cached_icon_is_not_downloaded_again(tmp_path: Path) -> None:
    session = Session()
    resolve(tmp_path, session)
    first = len(session.icon_calls)
    catalog = CooldownCatalog(tmp_path, session=session)
    catalog.loadouts(
        [RosterMember(champion_name="Ahri", participant_id="p1", champion_id="Ahri", level=11)]
    )
    assert len(session.icon_calls) == first, "the cache must survive a new catalog"


# --- every failure degrades to no icon ------------------------------------


def test_a_record_without_an_image_yields_no_icon(tmp_path: Path) -> None:
    session = Session()
    catalog = CooldownCatalog(tmp_path, session=session)
    loadout = catalog.loadouts(
        [RosterMember(champion_name="Naked", participant_id="p2", champion_id="Naked", level=11)]
    )[0]
    assert loadout.champion_icon_path is None
    assert loadout.ultimate.icon_path is None
    assert session.icon_calls == [], "nothing to request means no request"


def test_a_non_png_response_is_discarded(tmp_path: Path) -> None:
    loadout, _session = resolve(tmp_path, Session(icon=b"<html>404</html>"))
    assert loadout.champion_icon_path is None


def test_an_implausibly_large_icon_is_discarded(tmp_path: Path) -> None:
    loadout, _session = resolve(tmp_path, Session(icon=PNG_SIGNATURE + b"\x00" * (2 * 1024 * 1024)))
    assert loadout.champion_icon_path is None


def test_a_failed_request_yields_no_icon(tmp_path: Path) -> None:
    loadout, _session = resolve(tmp_path, Session(fail=True))
    assert loadout.champion_icon_path is None
    assert loadout.ultimate.icon_path is None


def test_a_failed_icon_does_not_lose_the_cooldowns(tmp_path: Path) -> None:
    """The whole point: art is optional, durations are not."""

    loadout, _session = resolve(tmp_path, Session(fail=True))
    assert loadout.ultimate.cooldowns == (130.0, 105.0, 80.0)
    assert loadout.summoner_spells[0].cooldowns == (300.0,)


def test_a_traversing_filename_is_refused(tmp_path: Path) -> None:
    """The filename comes off the network and becomes a path."""

    hostile = {
        "data": {
            "Evil": {
                "id": "Evil",
                "name": "Evil",
                "image": {"full": "../../../../Windows/System32/evil.png"},
                "spells": [{}, {}, {}, {"id": "EvilR", "maxrank": 3, "cooldown": [1.0, 2.0, 3.0]}],
            }
        }
    }

    class Hostile(Session):
        def get(self, url: str, **kwargs: Any) -> Any:
            if url.endswith("championFull.json"):
                return Response(json.dumps(hostile).encode())
            return super().get(url, **kwargs)

    session = Hostile()
    catalog = CooldownCatalog(tmp_path, session=session)
    loadout = catalog.loadouts(
        [RosterMember(champion_name="Evil", participant_id="p3", champion_id="Evil", level=11)]
    )[0]
    assert loadout.champion_icon_path is None
    assert session.icon_calls == []


def test_an_unwritable_cache_yields_no_icon(tmp_path: Path) -> None:
    blocked = tmp_path / "cache"
    blocked.write_text("i am a file, not a directory", encoding="utf-8")
    session = Session()
    catalog = CooldownCatalog(blocked, session=session)
    loadout = catalog.loadouts(
        [RosterMember(champion_name="Ahri", participant_id="p1", champion_id="Ahri", level=11)]
    )[0]
    assert loadout.champion_icon_path is None
