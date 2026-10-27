"""Sensor values: timestamp state, filters, attributes, local ticking."""

from __future__ import annotations

from datetime import timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.bvg_departures.api.models import DepartureBoard
from custom_components.bvg_departures.const import DOMAIN

from .conftest import (
    NOW,
    TAUERNALLEE,
    FakeBackend,
    board_from,
    make_entry,
    setup_entry,
    subentry,
)

LICHTENRADE = ("M76", "900070405", "M76 → Lichtenrade")


async def test_main_sensor(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None
    assert state.state == "2026-10-27T12:13:00+00:00"
    a = state.attributes
    assert a["friendly_name"] == "M76 → Lichtenrade"
    assert a["line"] == "M76" and a["product"] == "bus"
    assert a["headsign"] == "S Lichtenrade"
    assert a["realtime"] is True and a["cancelled"] is False
    assert a["platform_changed"] is False
    assert len(a["upcoming"]) == 3
    assert all(u["headsign"] == "S Lichtenrade" for u in a["upcoming"])
    assert a["backend"] == "transport_rest"

    # Device per line + direction, under the stop device.
    devices = dr.async_get(hass)
    entity = er.async_get(hass).async_get("sensor.m76_lichtenrade")
    assert entity is not None and entity.device_id is not None
    device = devices.async_get(entity.device_id)
    assert device is not None and device.name == "M76 → Lichtenrade"
    stop = devices.async_get_device_by_identifier((DOMAIN, TAUERNALLEE), entry.entry_id)
    assert stop is not None and device.via_device_id == stop.id
    assert stop.name == "Tauernallee/Säntisstr."
    # One device per subentry, plus the stop.
    assert len(dr.async_entries_for_config_entry(devices, entry.entry_id)) == 7


async def test_all_user_entities(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry())
    for entity_id, headsigns in {
        "sensor.m76_alt_mariendorf": {"U Walther-Schreiber-Platz via U Alt-Mariendorf"},
        "sensor.179_alt_mariendorf": {"U Alt-Mariendorf"},
        "sensor.x71_alt_mariendorf": {"U Alt-Mariendorf"},
        "sensor.x76_alt_mariendorf": {"U Alt-Mariendorf"},
        "sensor.m76_lichtenrade": {"S Lichtenrade"},
        "sensor.x76_nahariyastr": {"Lichtenrade, Nahariyastr."},
    }.items():
        state = hass.states.get(entity_id)
        assert state is not None, entity_id
        assert state.state.startswith("2026-10-27T1"), entity_id
        seen = {state.attributes["headsign"]} | {
            u["headsign"] for u in state.attributes["upcoming"]
        }
        assert seen == headsigns, entity_id


async def test_unknown_when_no_departures(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    fake_backend.board = DepartureBoard((), None, NOW, "transport_rest")
    await setup_entry(hass, make_entry([subentry(*LICHTENRADE)]))
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None and state.state == "unknown"
    assert "headsign" not in state.attributes


async def test_lead_time(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry([subentry(*LICHTENRADE, lead_min=15)]))
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None
    # 13:13 is sooner than 13:00 + 15 min walking -> skipped.
    assert state.state > "2026-10-27T12:15:00+00:00"


@pytest.mark.parametrize(("include", "expected"), [(False, "12:20"), (True, "12:15")])
async def test_cancelled_filter(
    hass: HomeAssistant,
    fake_backend: FakeBackend,
    freezer: FrozenDateTimeFactory,
    include: bool,
    expected: str,
) -> None:
    """Edge board: 179 at 13:15 is cancelled; no 179 later, X71 at 13:20 is not."""
    freezer.move_to(NOW)
    fake_backend.board = board_from("departures_edge_cases.json")
    cancelled = next(d for d in fake_backend.board.departures if d.cancelled)
    await setup_entry(
        hass,
        make_entry(
            [
                subentry(
                    cancelled.line.name,
                    None,
                    "Mixed",
                    match_mode="headsign",
                    headsigns=[cancelled.headsign],
                    include_cancelled=include,
                ),
                subentry("X71", None, "X71", match_mode="headsign", headsigns=["U Alt-Mariendorf"]),
            ]
        ),
    )
    state = hass.states.get("sensor.mixed")
    assert state is not None
    if include:
        assert state.state == f"2026-10-27T{expected}:00+00:00"
        assert state.attributes["cancelled"] is True
        assert state.attributes["remarks"] == ["Fahrt fällt aus"]
    else:
        assert state.state == "unknown"
    x71 = hass.states.get("sensor.x71")
    assert x71 is not None and x71.state == "2026-10-27T12:20:00+00:00"
    assert x71.attributes["delay_min"] is None


async def test_platform_change_and_delay(
    hass: HomeAssistant,
    fake_backend: FakeBackend,
    freezer: FrozenDateTimeFactory,
    entity_registry_enabled_by_default: None,
) -> None:
    freezer.move_to(NOW)
    fake_backend.board = board_from("departures_edge_cases.json")
    s41 = next(d for d in fake_backend.board.departures if d.line.name == "S41")
    delayed = next(d for d in fake_backend.board.departures if d.delay_s == 240)
    await setup_entry(
        hass,
        make_entry(
            [
                subentry(
                    "S41",
                    None,
                    "Ring",
                    product="suburban",
                    match_mode="headsign",
                    headsigns=[s41.headsign],
                ),
                subentry(
                    delayed.line.name,
                    None,
                    "Late",
                    match_mode="headsign",
                    headsigns=[delayed.headsign],
                ),
            ]
        ),
    )
    ring = hass.states.get("sensor.ring")
    assert ring is not None
    assert ring.attributes["platform"] == "11"
    assert ring.attributes["platform_changed"] is True
    late_delay = hass.states.get("sensor.late_delay")
    assert late_delay is not None and late_delay.state == "4"
    assert late_delay.attributes["unit_of_measurement"] == "min"


async def test_secondary_entities(
    hass: HomeAssistant,
    fake_backend: FakeBackend,
    freezer: FrozenDateTimeFactory,
    entity_registry_enabled_by_default: None,
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry([subentry(*LICHTENRADE)]))
    main = hass.states.get("sensor.m76_lichtenrade")
    following = hass.states.get("sensor.m76_lichtenrade_following_departure")
    delay = hass.states.get("sensor.m76_lichtenrade_delay")
    minutes = hass.states.get("sensor.m76_lichtenrade_minutes_until_departure")
    assert main and following and delay and minutes
    assert following.state > main.state
    assert delay.state == "0"
    assert minutes.state == "13"
    assert minutes.attributes["state_class"] == "measurement"


async def test_secondary_disabled_by_default(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry([subentry(*LICHTENRADE)]))
    reg = er.async_get(hass)
    for suffix in ("minutes_until_departure", "delay", "following_departure"):
        entry = reg.async_get(f"sensor.m76_lichtenrade_{suffix}")
        assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_minutes_tick_without_fetch(
    hass: HomeAssistant,
    fake_backend: FakeBackend,
    freezer: FrozenDateTimeFactory,
    entity_registry_enabled_by_default: None,
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry([subentry(*LICHTENRADE)]))
    entity_id = "sensor.m76_lichtenrade_minutes_until_departure"
    assert hass.states.get(entity_id).state == "13"  # type: ignore[union-attr]
    calls = len(fake_backend.calls)
    freezer.tick(timedelta(seconds=30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "12"  # type: ignore[union-attr]
    assert len(fake_backend.calls) == calls  # local tick, no API call
