"""Sensors: one set per departure subentry."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api.models import Departure
from .const import DOMAIN, PRODUCT_LABELS, PRODUCT_ORDER, SUBENTRY_DEPARTURE
from .coordinator import BvgConfigEntry, BvgCoordinator, match_departures

PARALLEL_UPDATES = 0
UPCOMING = 3


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BvgConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the entities of every departure subentry."""
    coordinator = entry.runtime_data
    for subentry in entry.subentries.values():
        if subentry.subentry_type != SUBENTRY_DEPARTURE:
            continue
        async_add_entities(
            [
                cls(coordinator, subentry)
                for cls in (
                    NextDepartureSensor,
                    MinutesUntilSensor,
                    DelaySensor,
                    FollowingDepartureSensor,
                )
            ],
            config_subentry_id=subentry.subentry_id,
        )


def _minutes(seconds: int | None) -> int | None:
    return None if seconds is None else round(seconds / 60)


class BvgSensor(CoordinatorEntity[BvgCoordinator], SensorEntity):
    """Base: matches the coordinator's boards against one subentry."""

    _attr_has_entity_name = True
    kind = ""

    def __init__(self, coordinator: BvgCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.cfg = coordinator.config_for(subentry)
        self._attr_unique_id = f"{entry.unique_id}:{subentry.unique_id}:{self.kind}"
        # A device belongs to exactly one subentry, so each line + direction is
        # its own device (named after the subentry title, which gives
        # sensor.m76_lichtenrade), linked to the stop device.
        product = PRODUCT_LABELS.get(self.cfg.product, self.cfg.product)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.unique_id}:{subentry.unique_id}")},
            name=subentry.title,
            entry_type=DeviceEntryType.SERVICE,
            manufacturer="VBB / BVG",
            model=f"{self.cfg.line_name} ({product})",
            via_device_id=coordinator.stop_device_id,
        )

    @property
    def candidates(self) -> list[Departure]:
        """Upcoming matching departures, re-evaluated on every call."""
        board = (self.coordinator.data or {}).get(self.cfg.board_key)
        return match_departures(board, self.cfg, dt_util.utcnow())

    @property
    def available(self) -> bool:
        """Unavailable without data, or when data is stale *and* exhausted."""
        data = self.coordinator.data or {}
        if self.cfg.board_key not in data:
            return False
        return not self.coordinator.is_stale(dt_util.utcnow()) or bool(self.candidates)


class NextDepartureSensor(BvgSensor):
    """State = next departure as a timestamp."""

    kind = "next"
    _attr_name = None  # the device (subentry title) names it
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _unrecorded_attributes = frozenset(
        {
            "product",
            "headsign",
            "planned",
            "realtime",
            "platform",
            "platform_changed",
            "cancelled",
            "remarks",
            "upcoming",
            "last_update",
            "backend",
        }
    )

    def __init__(self, coordinator: BvgCoordinator, subentry: ConfigSubentry) -> None:
        super().__init__(coordinator, subentry)
        product = self.cfg.product if self.cfg.product in PRODUCT_ORDER else "other"
        self._attr_translation_key = f"next_{product}"

    @property
    def native_value(self) -> datetime | None:
        cands = self.candidates
        return cands[0].effective if cands else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cands = self.candidates
        board = (self.coordinator.data or {}).get(self.cfg.board_key)
        attrs: dict[str, Any] = {
            "line": self.cfg.line_name,
            "product": self.cfg.product,
            "last_update": board.fetched_at.isoformat() if board else None,
            "backend": board.backend if board else None,
        }
        if not cands:
            return attrs
        first = cands[0]
        return attrs | {
            "headsign": first.headsign,
            "planned": first.planned.isoformat(),
            "delay_min": _minutes(first.delay_s),
            "realtime": first.when is not None,
            "platform": first.platform,
            "platform_changed": first.platform is not None
            and first.planned_platform is not None
            and first.platform != first.planned_platform,
            "cancelled": first.cancelled,
            "remarks": list(first.remarks),
            "upcoming": [
                {
                    "time": d.effective.isoformat(),
                    "planned": d.planned.isoformat(),
                    "delay_min": _minutes(d.delay_s),
                    "headsign": d.headsign,
                    "cancelled": d.cancelled,
                }
                for d in cands[1 : 1 + UPCOMING]
            ],
        }


class MinutesUntilSensor(BvgSensor):
    """Minutes until the next departure; recomputed locally every 30 s."""

    kind = "minutes"
    _attr_translation_key = "minutes"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> int | None:
        cands = self.candidates
        if not cands:
            return None
        return max(0, int((cands[0].effective - dt_util.utcnow()).total_seconds() // 60))


class DelaySensor(BvgSensor):
    """Delay of the next departure."""

    kind = "delay"
    _attr_translation_key = "delay"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> int | None:
        cands = self.candidates
        return _minutes(cands[0].delay_s) if cands else None


class FollowingDepartureSensor(BvgSensor):
    """The departure after the next one."""

    kind = "following"
    _attr_translation_key = "following"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_registry_enabled_default = False

    @property
    def native_value(self) -> datetime | None:
        cands = self.candidates
        return cands[1].effective if len(cands) > 1 else None
