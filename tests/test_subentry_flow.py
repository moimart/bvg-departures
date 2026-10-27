"""Subentry flow: "Add departure" and reconfigure."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bvg_departures.api.base import BackendUnavailable
from custom_components.bvg_departures.api.models import DepartureBoard, Line
from custom_components.bvg_departures.api.transport_rest import BASE_URL, TransportRestBackend
from custom_components.bvg_departures.config_flow import (
    build_direction_groups,
    line_sort_key,
    short_headsign,
)

from .conftest import (
    NOW,
    TAUERNALLEE,
    FakeBackend,
    board_from,
    load,
    make_entry,
    setup_entry,
    subentry,
)


def _options(result: Any, field: str) -> list[dict[str, str]]:
    for key, selector in result["data_schema"].schema.items():
        if key == field:
            options: list[dict[str, str]] = selector.config["options"]
            return options
    raise KeyError(field)


async def _add(hass: HomeAssistant, entry_id: str) -> Any:
    return await hass.config_entries.subentries.async_init(
        (entry_id, "departure"), context={"source": SOURCE_USER}
    )


async def _configure(hass: HomeAssistant, result: Any, data: dict[str, Any]) -> Any:
    return await hass.config_entries.subentries.async_configure(result["flow_id"], data)


def test_line_sorting() -> None:
    lines = [
        Line("100", "bus"),
        Line("M17", "tram"),
        Line("M10", "tram"),
        Line("U6", "subway"),
        Line("M76", "bus"),
        Line("S41", "suburban"),
        Line("FEX", "regional"),
        Line("X76", "bus"),
        Line("M10", "bus"),
    ]
    assert [(ln.name, ln.product) for ln in sorted(lines, key=line_sort_key)] == [
        ("S41", "suburban"),
        ("U6", "subway"),
        ("M10", "tram"),
        ("M17", "tram"),
        ("M10", "bus"),
        ("M76", "bus"),
        ("X76", "bus"),
        ("100", "bus"),
        ("FEX", "regional"),
    ]


def test_short_headsign() -> None:
    assert (
        short_headsign("U Walther-Schreiber-Platz via U Alt-Mariendorf")
        == "Walther-Schreiber-Platz"
    )
    assert short_headsign("S+U Hermannstr.") == "Hermannstr."
    assert short_headsign("Lichtenrade, Nahariyastr.") == "Lichtenrade, Nahariyastr."


async def test_add_departure(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry([]))
    result = await _add(hass, entry.entry_id)
    assert result["step_id"] == "line"
    result = await _configure(hass, result, {"line": "bus:X76"})
    assert result["step_id"] == "direction"
    assert {o["value"] for o in _options(result, "direction")} == {
        "stop:900074101",
        "stop:900070702",
    }
    result = await _configure(hass, result, {"direction": "stop:900074101"})
    assert result["step_id"] == "details"
    result = await _configure(
        hass, result, {"name": "X76 → Nahariyastr.", "lead_min": 4, "include_cancelled": True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    (sub,) = entry.subentries.values()
    assert sub.unique_id == "bus:X76:900074101"
    assert sub.data["lead_min"] == 4 and sub.data["include_cancelled"] is True
    # Entry reloaded by the update listener: the entity exists and has data.
    state = hass.states.get("sensor.x76_nahariyastr")
    assert state is not None and state.state not in ("unknown", "unavailable")


async def test_duplicate_aborts(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    entry = await setup_entry(hass, make_entry())
    result = await _add(hass, entry.entry_id)
    result = await _configure(hass, result, {"line": "bus:M76"})
    result = await _configure(hass, result, {"direction": "stop:900070405"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_single_direction_skips_picker(
    hass: HomeAssistant, fake_backend: FakeBackend
) -> None:
    fake_backend.board = DepartureBoard(
        tuple(d for d in fake_backend.board.departures if d.headsign == "S Lichtenrade"),
        None,
        NOW,
        "transport_rest",
    )
    entry = await setup_entry(hass, make_entry([]))
    result = await _add(hass, entry.entry_id)
    result = await _configure(hass, result, {"line": "bus:M76"})
    assert result["step_id"] == "details"


async def test_no_live_departures(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    entry = await setup_entry(hass, make_entry([]))
    result = await _add(hass, entry.entry_id)
    # N81 is a night line: listed at the stop, no departures in the next 2 h.
    result = await _configure(hass, result, {"line": "bus:N81"})
    assert result["step_id"] == "direction"
    assert result["errors"] == {"base": "no_live_departures"}
    calls = len(fake_backend.calls)
    result = await _configure(hass, result, {})  # "try again" refetches
    assert result["errors"] == {"base": "no_live_departures"}
    assert ("departures", None) in fake_backend.calls[calls:]
    fake_backend.fail = BackendUnavailable("down")
    result = await _configure(hass, result, {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_lines_unreachable(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    entry = await setup_entry(hass, make_entry([]))
    fake_backend.fail = BackendUnavailable("down")
    result = await _add(hass, entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_no_lines(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    entry = await setup_entry(hass, make_entry([]))
    fake_backend.board = DepartureBoard((), None, NOW, "x")

    async def no_lines(stop_id: str) -> list[Line]:
        return []

    fake_backend.lines_at_stop = no_lines  # type: ignore[method-assign]
    result = await _add(hass, entry.entry_id)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_lines"


async def test_short_turn_grouped() -> None:
    """Two headsigns with the same next stop -> one option, most frequent first."""
    backend = FakeBackend()
    backend.next_stops["M76|U Walther-Schreiber-Platz via U Alt-Mariendorf"] = "900070405"
    deps = [d for d in backend.board.departures if d.line.name == "M76"]
    groups = await build_direction_groups(backend, TAUERNALLEE, Line("M76", "bus"), deps)  # type: ignore[arg-type]
    assert len(groups) == 1
    assert set(groups[0].headsigns) == {
        "S Lichtenrade",
        "U Walther-Schreiber-Platz via U Alt-Mariendorf",
    }
    assert groups[0].label.startswith("→ ")
    assert " / " in groups[0].label


async def test_unresolved_next_stop_falls_back_to_headsign(
    hass: HomeAssistant, fake_backend: FakeBackend
) -> None:
    del fake_backend.next_stops["X76|U Alt-Mariendorf"]
    entry = await setup_entry(hass, make_entry([]))
    result = await _add(hass, entry.entry_id)
    result = await _configure(hass, result, {"line": "bus:X76"})
    options = {o["value"]: o["label"] for o in _options(result, "direction")}
    assert options["headsign:U Alt-Mariendorf"] == "→ Alt-Mariendorf"
    result = await _configure(hass, result, {"direction": "headsign:U Alt-Mariendorf"})
    result = await _configure(
        hass, result, {"name": "X76 → Alt-Mariendorf", "lead_min": 0, "include_cancelled": False}
    )
    await hass.async_block_till_done()
    (sub,) = entry.subentries.values()
    assert sub.data["match_mode"] == "headsign"
    assert sub.data["headsigns"] == ["U Alt-Mariendorf"]
    assert sub.data["direction_stop_id"] is None


async def test_ringbahn_directions(aioclient_mock: AiohttpClientMocker) -> None:
    """S41 and S42 at Südkreuz resolve to different next stops via real trips."""
    board = board_from("departures_suedkreuz.json")
    for dep in board.departures:
        trip = load(f"transport_rest/trip_{dep.line.name.lower()}.json")
        aioclient_mock.get(f"{BASE_URL}/trips/{quote(dep.trip_id, safe='')}", json=trip)
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    backend = TransportRestBackend(session)
    result = {}
    for name in ("S41", "S42"):
        line = Line(name, "suburban")
        deps = [d for d in board.departures if d.line == line]
        (group,) = await build_direction_groups(backend, "900058101", line, deps)
        result[name] = group
    await session.close()
    assert result["S41"].next_stop_id == "900054104"  # S Schöneberg
    assert result["S42"].next_stop_id == "900068201"  # S+U Tempelhof
    assert result["S41"].headsigns == ["Ringbahn S41 ⟳"]
    # Concurrency cap: lookups are one per headsign, not one per departure.
    assert aioclient_mock.call_count == 2


async def test_reconfigure_keeps_entity_id(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    """Acceptance 5: renaming / walking time never changes the entity_id."""
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    reg = er.async_get(hass)
    sub = next(s for s in entry.subentries.values() if s.title == "M76 → Lichtenrade")
    before = {
        e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)
    }
    assert hass.states.get("sensor.m76_lichtenrade") is not None

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "departure"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id},
    )
    assert result["step_id"] == "reconfigure"
    headsigns = [o["value"] if isinstance(o, dict) else o for o in _section_options(result)]
    assert headsigns == ["S Lichtenrade", "U Walther-Schreiber-Platz via U Alt-Mariendorf"]
    result = await _configure(
        hass,
        result,
        {
            "name": "Bus nach Lichtenrade",
            "lead_min": 5,
            "include_cancelled": False,
            "advanced": {"match_by_headsign": False, "headsigns": []},
        },
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()

    after = {
        e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(reg, entry.entry_id)
    }
    assert after == before
    sub = entry.subentries[sub.subentry_id]
    assert sub.title == "Bus nach Lichtenrade" and sub.data["lead_min"] == 5
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None
    assert state.attributes["friendly_name"].endswith("Bus nach Lichtenrade")
    # 13:13 departure is within 5 min walking time of 13:00? No: 13:13 >= 13:05.
    assert state.state == "2026-10-27T12:13:00+00:00"


def _section_options(result: Any) -> list[Any]:
    for key, value in result["data_schema"].schema.items():
        if key == "advanced":
            for inner_key, selector in value.schema.schema.items():
                if inner_key == "headsigns":
                    options: list[Any] = selector.config["options"]
                    return options
    raise KeyError("advanced.headsigns")


@pytest.mark.parametrize(
    ("advanced", "direction", "error"),
    [
        ({"match_by_headsign": True, "headsigns": []}, "900070405", "headsigns_required"),
        ({"match_by_headsign": False, "headsigns": []}, None, "headsign_only"),
    ],
)
async def test_reconfigure_errors(
    hass: HomeAssistant,
    fake_backend: FakeBackend,
    advanced: dict[str, Any],
    direction: str | None,
    error: str,
) -> None:
    entry = await setup_entry(
        hass,
        make_entry(
            [
                subentry(
                    "M76",
                    direction,
                    "M76 → Lichtenrade",
                    match_mode="next_stop" if direction else "headsign",
                    headsigns=[] if direction else ["S Lichtenrade"],
                )
            ]
        ),
    )
    (sub,) = entry.subentries.values()
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "departure"),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id},
    )
    result = await _configure(
        hass,
        result,
        {"name": "x", "lead_min": 0, "include_cancelled": False, "advanced": advanced},
    )
    assert result["errors"] == {"base": error}


async def test_reconfigure_to_headsign_mode(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    sub = next(s for s in entry.subentries.values() if s.title == "M76 → Lichtenrade")
    context = {"source": SOURCE_RECONFIGURE, "subentry_id": sub.subentry_id}
    fake_backend.fail = BackendUnavailable("down")  # headsign list falls back to stored ones
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "departure"), context=context
    )
    assert _section_options(result) == []
    hass.config_entries.subentries.async_abort(result["flow_id"])
    fake_backend.fail = None
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "departure"), context=context
    )
    result = await _configure(
        hass,
        result,
        {
            "name": "M76 → Lichtenrade",
            "lead_min": 0,
            "include_cancelled": False,
            "advanced": {"match_by_headsign": True, "headsigns": ["S Lichtenrade"]},
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    data = entry.subentries[sub.subentry_id].data
    assert data["match_mode"] == "headsign" and data["headsigns"] == ["S Lichtenrade"]
    # Headsign mode reads the unfiltered board.
    assert ("departures", None) in fake_backend.calls
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None and state.state == "2026-10-27T12:13:00+00:00"
