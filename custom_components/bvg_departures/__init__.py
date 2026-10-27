"""BVG Departures: next departure per stop + line + direction."""

from __future__ import annotations

from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.service import async_extract_config_entry_ids
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.hass_dict import HassKey

from .api import create_backend
from .api.ratelimit import TokenBucket
from .const import CONF_BACKEND, DEFAULT_BACKEND, DOMAIN, LOCAL_TICK
from .coordinator import BvgConfigEntry, BvgCoordinator

PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SERVICE_REFRESH = "refresh"

# Shared by every entry: transport.rest allows ~100 req/min in total; stay at 60.
LIMITER_KEY: HassKey[TokenBucket] = HassKey(f"{DOMAIN}_limiter")


def get_limiter(hass: HomeAssistant) -> TokenBucket:
    """Return the integration-wide request limiter."""
    if LIMITER_KEY not in hass.data:
        hass.data[LIMITER_KEY] = TokenBucket(rate=60, period=60)
    return hass.data[LIMITER_KEY]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the refresh service."""

    async def _refresh(call: ServiceCall) -> None:
        for entry_id in await async_extract_config_entry_ids(call):
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and entry.domain == DOMAIN and hasattr(entry, "runtime_data"):
                await entry.runtime_data.async_refresh()

    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _refresh)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BvgConfigEntry) -> bool:
    """Set up one stop."""
    backend = create_backend(
        async_get_clientsession(hass),
        entry.options.get(CONF_BACKEND, DEFAULT_BACKEND),
        get_limiter(hass),
    )
    coordinator = BvgCoordinator(hass, entry, backend)
    if coordinator.departure_configs:
        await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    # Parent device for the per-departure devices (see sensor.py).
    coordinator.stop_device_id = (
        dr.async_get(hass)
        .async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, str(entry.unique_id))},
            name=coordinator.stop_name,
            entry_type=dr.DeviceEntryType.SERVICE,
            manufacturer="VBB / BVG",
        )
        .id
    )

    # Re-evaluate entities between fetches: drops departures that have left
    # and ticks the "minutes until" sensor without any API call.
    @callback
    def _tick(now: datetime) -> None:
        if coordinator.data:
            coordinator.async_update_listeners()

    entry.async_on_unload(
        async_track_time_interval(hass, _tick, LOCAL_TICK, cancel_on_shutdown=True)
    )
    # Options changes and subentry add/edit/remove all land here.
    entry.async_on_unload(entry.add_update_listener(_async_reload))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_reload(hass: HomeAssistant, entry: BvgConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BvgConfigEntry) -> bool:
    """Unload a stop."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
