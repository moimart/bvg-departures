"""Coordinator: dedup, adaptive polling, failover, backoff, stale cache, repairs."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.bvg_departures.api import FailoverBackend
from custom_components.bvg_departures.api.base import BackendUnavailable, RateLimited
from custom_components.bvg_departures.const import DOMAIN
from custom_components.bvg_departures.diagnostics import async_get_config_entry_diagnostics

from .conftest import NOW, USER_SETUP, FakeBackend, make_entry, setup_entry, subentry


def _directions(backend: FakeBackend) -> list[str | None]:
    return [c[1] for c in backend.calls if c[0] == "departures"]


async def test_one_request_per_direction(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    """Six departures, four distinct next stops -> four requests per cycle."""
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.LOADED
    assert sorted(_directions(fake_backend)) == sorted(
        ["900070452", "900070702", "900070405", "900074101"]
    )


async def test_shared_direction_adds_no_request(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    """Acceptance 2: 179 shares M76's direction -> same number of requests."""
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry([subentry(*USER_SETUP[0])]))
    one = len(_directions(fake_backend))
    await hass.config_entries.async_unload(entry.entry_id)
    fake_backend.calls.clear()
    hass.config_entries.async_update_entry(entry, unique_id="other")  # avoid duplicate
    await setup_entry(hass, make_entry([subentry(*USER_SETUP[0]), subentry(*USER_SETUP[1])]))
    assert len(_directions(fake_backend)) == one == 1


async def test_headsign_mode_uses_unfiltered_board(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await setup_entry(
        hass,
        make_entry(
            [
                subentry("M76", None, "a", match_mode="headsign", headsigns=["S Lichtenrade"]),
                subentry("X76", None, "b", match_mode="headsign", headsigns=["U Alt-Mariendorf"]),
            ]
        ),
    )
    assert _directions(fake_backend) == [None]


async def test_adaptive_interval(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)  # 13:00, first bus at 13:06 -> base interval
    entry = await setup_entry(hass, make_entry())
    coordinator = entry.runtime_data
    assert coordinator.update_interval == timedelta(seconds=60)

    freezer.move_to(NOW - timedelta(minutes=30))  # 12:30, next relevant one 36 min away
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=300)

    freezer.move_to(NOW - timedelta(minutes=14))  # 21 min away: still slow
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=300)

    freezer.move_to(NOW - timedelta(minutes=12))  # 19 min away: back to base
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=60)


async def test_slow_interval_never_below_base(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW - timedelta(minutes=30))
    entry = await setup_entry(hass, make_entry(update_interval_s=600))
    assert entry.runtime_data.update_interval == timedelta(seconds=600)


async def test_backoff_and_retry_after(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    coordinator = entry.runtime_data
    fake_backend.fail = BackendUnavailable("down")
    seen = []
    for _ in range(5):
        await coordinator.async_refresh()
        assert coordinator.update_interval is not None
        seen.append(coordinator.update_interval.total_seconds())
    assert seen == [60, 120, 240, 600, 600]
    # Cached data keeps the coordinator "successful"; entities decide availability.
    assert coordinator.last_update_success

    fake_backend.fail = RateLimited(90)
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=90)

    fake_backend.fail = None
    await coordinator.async_refresh()
    assert coordinator.update_interval == timedelta(seconds=60)
    assert coordinator.consecutive_failures == 0


async def test_failover_keeps_entities_valid(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Killing the primary: values come from the fallback, attribute says so."""
    freezer.move_to(NOW)
    primary, fallback = FakeBackend(), FakeBackend()
    primary.fail = BackendUnavailable("transport.rest down")
    fallback.name = "mgate"
    failover = FailoverBackend(primary, fallback)  # type: ignore[arg-type]
    with patch("custom_components.bvg_departures.create_backend", return_value=failover):
        entry = await setup_entry(hass, make_entry())
    state = hass.states.get("sensor.m76_lichtenrade")
    assert state is not None and state.state == "2026-10-27T12:13:00+00:00"
    assert state.attributes["backend"] == "mgate"
    assert failover.failovers == 4
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["backend"]["failovers"] == 4
    assert set(diag["backend"]["backend_used"].values()) == {"mgate"}


async def test_first_refresh_failure_retries_setup(
    hass: HomeAssistant, fake_backend: FakeBackend
) -> None:
    fake_backend.fail = BackendUnavailable("down")
    entry = await setup_entry(hass, make_entry())
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_stale_cache_keeps_advancing_then_goes_unavailable(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    """Acceptance 3 (cache part): state advances on cached data, no API calls."""
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    first = hass.states.get("sensor.m76_lichtenrade")
    assert first is not None and first.state == "2026-10-27T12:13:00+00:00"

    fake_backend.fail = BackendUnavailable("down")
    freezer.move_to(NOW + timedelta(minutes=14))  # the 13:13 bus has left
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    later = hass.states.get("sensor.m76_lichtenrade")
    assert later is not None
    assert later.state not in ("unavailable", "unknown")
    assert later.state > first.state

    # Repairs issue appears after >15 min of total outage...
    for minutes in (20, 40):
        freezer.move_to(NOW + timedelta(minutes=minutes))
        await entry.runtime_data.async_refresh()
    issue_id = f"api_unreachable_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None

    # ... and once the cache runs dry, entities go unavailable.
    freezer.move_to(NOW + timedelta(hours=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    gone = hass.states.get("sensor.m76_lichtenrade")
    assert gone is not None and gone.state == "unavailable"

    # Recovery clears the issue.
    fake_backend.fail = None
    freezer.move_to(NOW)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None
    assert hass.states.get("sensor.m76_lichtenrade").state != "unavailable"  # type: ignore[union-attr]


async def test_refresh_service(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    await setup_entry(hass, make_entry())
    before = len(fake_backend.calls)
    await hass.services.async_call(
        DOMAIN, "refresh", {"entity_id": "sensor.m76_lichtenrade"}, blocking=True
    )
    assert len(fake_backend.calls) == before + 4


async def test_diagnostics_and_unload(
    hass: HomeAssistant, fake_backend: FakeBackend, freezer: FrozenDateTimeFactory
) -> None:
    freezer.move_to(NOW)
    entry = await setup_entry(hass, make_entry())
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert len(diag["subentries"]) == 6
    assert set(diag["boards"]) == {"900070452", "900070702", "900070405", "900074101"}
    assert all(len(b["departures"]) <= 10 for b in diag["boards"].values())
    assert diag["backend"]["requests"] == 4 and diag["backend"]["failures"] == 0
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
