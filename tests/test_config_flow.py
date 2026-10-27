"""Main flow: search -> pick stop -> first line -> direction -> details."""

from __future__ import annotations

from typing import Any

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
import pytest

from custom_components.bvg_departures.api.base import BackendUnavailable
from custom_components.bvg_departures.const import DOMAIN

from .conftest import NOW, TAUERNALLEE, FakeBackend, make_entry, setup_entry


def _options(result: Any, field: str) -> list[dict[str, str]]:
    for key, selector in result["data_schema"].schema.items():
        if key == field:
            options: list[dict[str, str]] = selector.config["options"]
            return options
    raise KeyError(field)


async def _start(hass: HomeAssistant, query: str) -> Any:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"stop_name": query})


async def test_full_flow_creates_entry_with_subentry(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    """Acceptance 1 & 4: pickers only; one working timestamp entity at the end."""
    freezer.move_to(NOW)
    result = await _start(hass, "Tauernallee")
    assert result["step_id"] == "pick_stop"
    labels = [o["label"] for o in _options(result, "stop")]
    assert labels[0] == "Tauernallee/Säntisstr. · M76 N81 X71 X76 179"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": TAUERNALLEE}
    )
    assert result["step_id"] == "first_line"
    assert [o["label"] for o in _options(result, "line")] == [
        "M76 (Bus)",
        "N81 (Bus)",
        "X71 (Bus)",
        "X76 (Bus)",
        "179 (Bus)",
    ]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"line": "bus:M76"})
    assert result["step_id"] == "direction"
    options = {o["value"]: o["label"] for o in _options(result, "direction")}
    assert options == {
        "stop:900070452": "→ Walther-Schreiber-Platz  (Stop 900070452)",
        "stop:900070405": "→ Lichtenrade  (Stop 900070405)",
    }

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"direction": "stop:900070405"}
    )
    assert result["step_id"] == "details"
    defaults = {str(k): k.default() for k in result["data_schema"].schema}
    assert defaults == {"name": "M76 → Lichtenrade", "lead_min": 0, "include_cancelled": False}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"name": "M76 → Lichtenrade", "lead_min": 0, "include_cancelled": False}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Tauernallee/Säntisstr."
    assert result["data"] == {"stop_id": TAUERNALLEE, "stop_name": "Tauernallee/Säntisstr."}
    assert result["options"] == {"backend": "auto", "update_interval_s": 60, "window_min": 90}
    entry = result["result"]
    (sub,) = entry.subentries.values()
    assert sub.unique_id == "bus:M76:900070405"
    assert sub.title == "M76 → Lichtenrade"
    assert sub.data["direction_stop_id"] == "900070405"
    assert sub.data["match_mode"] == "next_stop"
    assert sub.data["direction_label"] == "Lichtenrade"
    await hass.async_block_till_done()

    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None
    assert state.state == "2026-10-27T12:13:00+00:00"
    assert state.attributes["device_class"] == "timestamp"
    assert state.attributes["headsign"] == "S Lichtenrade"
    entity = er.async_get(hass).async_get("sensor.m76_lichtenrade")
    assert entity is not None and entity.config_subentry_id == sub.subentry_id


async def test_single_result_skips_picker(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    result = await _start(hass, "Tauernallee/Säntisstr.")
    assert result["step_id"] == "first_line"


async def test_no_results(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    result = await _start(hass, "nothing")
    assert result["step_id"] == "user"
    assert result["errors"] == {"stop_name": "no_stops_found"}


async def test_search_unreachable(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    fake_backend.fail = BackendUnavailable("down")
    result = await _start(hass, "Tauernallee")
    assert result["errors"] == {"base": "cannot_connect"}


async def test_skip_lines(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    result = await _start(hass, "Tauernallee/Säntisstr.")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert not result["result"].subentries
    await hass.async_block_till_done()
    # Nothing to watch -> no polling at all.
    assert result["result"].runtime_data.update_interval is None
    assert not [c for c in fake_backend.calls if c[0] == "departures" and c[1]]


async def test_lines_unreachable_aborts(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    result = await _start(hass, "Tauernallee")
    fake_backend.fail = BackendUnavailable("down")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"stop": TAUERNALLEE}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "cannot_connect"


async def test_stop_already_configured(hass: HomeAssistant, fake_backend: FakeBackend) -> None:
    await setup_entry(hass, make_entry([]))
    result = await _start(hass, "Tauernallee/Säntisstr.")
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


@pytest.mark.parametrize(
    "user_input", [{"backend": "mgate", "update_interval_s": 120, "window_min": 60}]
)
async def test_options_flow(
    hass: HomeAssistant, fake_backend: FakeBackend, user_input: dict[str, Any]
) -> None:
    entry = await setup_entry(hass, make_entry())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert dict(entry.options) == user_input
    # Reloaded with the new interval.
    assert entry.runtime_data.base_interval.total_seconds() == 120
