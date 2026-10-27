"""Shared fixtures: recorded responses and a fixture-backed fake backend."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import PropertyMock, patch

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.bvg_departures.api.base import BackendError
from custom_components.bvg_departures.api.models import (
    DepartureBoard,
    Line,
    Stop,
)
from custom_components.bvg_departures.api.transport_rest import _stop, parse_departure
from custom_components.bvg_departures.const import DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
TAUERNALLEE = "900070401"
# Recording was taken 2026-10-27 ~13:05 CET; first departure 13:06.
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # 13:00 CET


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


def load(name: str) -> Any:
    """Load a JSON fixture by relative path."""
    return json.loads((FIXTURES / name).read_text())


def board_from(name: str, backend: str = "transport_rest") -> DepartureBoard:
    """Parse a transport.rest departures fixture into a board."""
    raw = load(f"transport_rest/{name}")
    return DepartureBoard(
        departures=tuple(parse_departure(d) for d in raw["departures"]),
        realtime_updated=None,
        fetched_at=NOW,
        backend=backend,
    )


def tauernallee_next_stops() -> dict[str, str]:
    """ "line|headsign" -> next stop after Tauernallee, resolved live via /trips."""
    result: dict[str, str] = load("transport_rest/tauernallee_next_stops.json")
    return result


class FakeBackend:
    """Serves Tauernallee from recorded fixtures; filters by direction like the API."""

    name = "transport_rest"

    def __init__(self, board: DepartureBoard | None = None) -> None:
        self.board = board or board_from("departures_tauernallee.json")
        self.next_stops = tauernallee_next_stops()
        self.calls: list[tuple[str, Any]] = []
        self.fail: BackendError | None = None
        self.stops = [_stop(s) for s in load("transport_rest/locations_tauernallee.json")]

    def _check(self) -> None:
        if self.fail is not None:
            raise self.fail

    def next_stop_of(self, line: str, headsign: str) -> str | None:
        return self.next_stops.get(f"{line}|{headsign}")

    async def search_stops(self, query: str, limit: int = 8) -> list[Stop]:
        self.calls.append(("search", query))
        self._check()
        if query == "nothing":
            return []
        if query == "Tauernallee/Säntisstr.":
            return self.stops[:1]
        return self.stops

    async def lines_at_stop(self, stop_id: str) -> list[Line]:
        self.calls.append(("lines", stop_id))
        self._check()
        return [Line(n, "bus") for n in ("179", "M76", "N81", "X71", "X76")]

    async def departures(
        self, stop_id: str, *, direction_stop_id: str | None, duration_min: int, max_results: int
    ) -> DepartureBoard:
        self.calls.append(("departures", direction_stop_id))
        self._check()
        deps = self.board.departures
        if direction_stop_id:
            deps = tuple(
                d for d in deps if self.next_stop_of(d.line.name, d.headsign) == direction_stop_id
            )
        return DepartureBoard(deps, None, NOW, self.name)

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Stop | None:
        self.calls.append(("trip", trip_id))
        self._check()
        dep = next(d for d in self.board.departures if d.trip_id == trip_id)
        stop_id = self.next_stop_of(dep.line.name, dep.headsign)
        return Stop(stop_id, f"Stop {stop_id}", frozenset({"bus"})) if stop_id else None


@pytest.fixture
def entity_registry_enabled_by_default() -> Generator[None]:
    """Enable entities that are disabled by default."""
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def fake_backend() -> Generator[FakeBackend]:
    """Patch backend creation everywhere to return one FakeBackend."""
    backend = FakeBackend()
    with (
        patch("custom_components.bvg_departures.create_backend", return_value=backend),
        patch("custom_components.bvg_departures.config_flow.create_backend", return_value=backend),
    ):
        yield backend


# The six departures from the README example (Tauernallee/Säntisstr.).
USER_SETUP = [
    ("M76", "900070452", "M76 → Alt-Mariendorf"),
    ("179", "900070452", "179 → Alt-Mariendorf"),
    ("X71", "900070702", "X71 → Alt-Mariendorf"),
    ("X76", "900070702", "X76 → Alt-Mariendorf"),
    ("M76", "900070405", "M76 → Lichtenrade"),
    ("X76", "900074101", "X76 → Nahariyastr."),
]


def subentry(line: str, direction: str | None, title: str, **data: Any) -> dict[str, Any]:
    """Subentry data as the flow stores it."""
    return {
        "data": {
            "line_name": line,
            "product": "bus",
            "direction_stop_id": direction,
            "direction_label": title.rsplit("→ ", maxsplit=1)[-1],
            "next_stop_name": "x",
            "match_mode": "next_stop",
            "headsigns": [],
            "lead_min": 0,
            "include_cancelled": False,
        }
        | data,
        "subentry_type": "departure",
        "title": title,
        "unique_id": f"bus:{line}:{direction or 'headsign'}",
    }


def make_entry(subentries: list[dict[str, Any]] | None = None, **options: Any) -> MockConfigEntry:
    """A Tauernallee config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Tauernallee/Säntisstr.",
        unique_id=TAUERNALLEE,
        data={"stop_id": TAUERNALLEE, "stop_name": "Tauernallee/Säntisstr."},
        options={"backend": "auto", "update_interval_s": 60, "window_min": 90} | options,
        subentries_data=subentries
        if subentries is not None
        else [subentry(*row) for row in USER_SETUP],
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Add and set up an entry."""
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
