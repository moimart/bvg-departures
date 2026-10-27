"""Config flow: stop -> line -> direction -> details, with pickers throughout."""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import logging
import re
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryData,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
import probatio as vol

from . import get_limiter
from .api import BACKENDS, create_backend
from .api.base import BackendError, DepartureBackend
from .api.models import Departure, DepartureBoard, Line, Stop
from .const import (
    CONF_BACKEND,
    CONF_DIRECTION_LABEL,
    CONF_DIRECTION_STOP_ID,
    CONF_HEADSIGNS,
    CONF_INCLUDE_CANCELLED,
    CONF_LEAD_MIN,
    CONF_LINE_NAME,
    CONF_MATCH_MODE,
    CONF_NEXT_STOP_NAME,
    CONF_PRODUCT,
    CONF_STOP_ID,
    CONF_STOP_NAME,
    CONF_UPDATE_INTERVAL,
    CONF_WINDOW,
    DEFAULT_BACKEND,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WINDOW,
    DISCOVERY_WINDOW_MIN,
    DOMAIN,
    MATCH_HEADSIGN,
    MATCH_NEXT_STOP,
    PRODUCT_LABELS,
    PRODUCT_ORDER,
    SUBENTRY_DEPARTURE,
)

_LOGGER = logging.getLogger(__name__)

CONF_QUERY = "stop_name"
CONF_STOP = "stop"
CONF_LINE = "line"
CONF_DIRECTION = "direction"
CONF_NAME = "name"
CONF_ADVANCED = "advanced"
CONF_MATCH_BY_HEADSIGN = "match_by_headsign"

TRIP_LOOKUP_CONCURRENCY = 5
STOP_LABEL_LINES = 5

_HEADSIGN_PREFIX = re.compile(r"^(S\+U|S|U)\s+")
_HEADSIGN_VIA = re.compile(r"\s+via\s+.*$", re.IGNORECASE)
_NUMBER = re.compile(r"(\d+)")


def short_headsign(headsign: str) -> str:
    """'U Walther-Schreiber-Platz via S Priesterweg' -> 'Walther-Schreiber-Platz'."""
    return _HEADSIGN_PREFIX.sub("", _HEADSIGN_VIA.sub("", headsign)).strip() or headsign


def line_name_key(name: str) -> tuple[Any, ...]:
    """Natural sort with lettered lines first: M10 < M17 < X76 < 100 < 179."""
    parts = [int(p) if p.isdigit() else p for p in _NUMBER.split(name) if p]
    return (0 if name[:1].isalpha() else 1, [(isinstance(p, int), p) for p in parts])


def line_sort_key(line: Line) -> tuple[Any, ...]:
    """Product order (S, U, Tram, Bus, other), then natural name order."""
    rank = PRODUCT_ORDER.index(line.product) if line.product in PRODUCT_ORDER else 99
    return (rank, line_name_key(line.name))


def line_key(line: Line) -> str:
    """Select option value for a line."""
    return f"{line.product}:{line.name}"


def line_label(line: Line) -> str:
    """'U6 (U-Bahn)'."""
    return f"{line.name} ({PRODUCT_LABELS.get(line.product, line.product)})"


def stop_label(stop: Stop) -> str:
    """'Tauernallee/Säntisstr. · M76 X71 X76 179…'."""
    names = sorted(stop.lines, key=line_name_key)
    if not names:
        names = [PRODUCT_LABELS.get(p, p) for p in PRODUCT_ORDER if p in stop.products]
    if not names:
        return stop.name
    more = "…" if len(names) > STOP_LABEL_LINES else ""
    return f"{stop.name} · {' '.join(names[:STOP_LABEL_LINES])}{more}"


@dataclass
class DirectionGroup:
    """Headsigns of one line that share the stop after ours."""

    next_stop_id: str | None
    next_stop_name: str | None
    headsigns: list[str]  # most frequent first

    @property
    def key(self) -> str:
        if self.next_stop_id:
            return f"stop:{self.next_stop_id}"
        return f"headsign:{self.headsigns[0]}"

    @property
    def label(self) -> str:
        heads = " / ".join(short_headsign(h) for h in self.headsigns)
        return f"→ {heads}  ({self.next_stop_name})" if self.next_stop_name else f"→ {heads}"


def subentry_unique_id(line: Line, group: DirectionGroup) -> str:
    """Spec 6.2: {product}:{line_name}:{direction_stop_id}."""
    direction = group.next_stop_id or f"headsign:{group.headsigns[0]}"
    return f"{line.product}:{line.name}:{direction}"


async def build_direction_groups(
    backend: DepartureBackend, stop_id: str, line: Line, departures: list[Departure]
) -> list[DirectionGroup]:
    """Group a line's headsigns by the next stop after `stop_id` (spec 5, 7.2)."""
    counts = Counter(d.headsign for d in departures)
    sample: dict[str, Departure] = {}
    for dep in departures:
        sample.setdefault(dep.headsign, dep)
    semaphore = asyncio.Semaphore(TRIP_LOOKUP_CONCURRENCY)

    async def next_stop(dep: Departure) -> Stop | None:
        async with semaphore:
            try:
                # Bus trips list the bay the departure leaves from, not the station.
                for after in dict.fromkeys((dep.stop_id or stop_id, stop_id)):
                    if (found := await backend.trip_next_stop(dep.trip_id, after)) is not None:
                        return found
            except BackendError as err:
                _LOGGER.debug("Next stop of %s unknown: %s", dep.trip_id, err)
            return None

    heads = sorted(sample, key=lambda h: (-counts[h], h))
    resolved = await asyncio.gather(*(next_stop(sample[h]) for h in heads))
    groups: dict[str, DirectionGroup] = {}
    for head, stop in zip(heads, resolved, strict=True):
        if stop is None:
            # Can't tell the next stop: fall back to matching this headsign.
            group = DirectionGroup(None, None, [head])
            groups.setdefault(group.key, group)
            continue
        group = groups.setdefault(f"stop:{stop.id}", DirectionGroup(stop.id, stop.name, []))
        group.headsigns.append(head)
    return list(groups.values())


def _details_schema(name: str, lead_min: int, include_cancelled: bool) -> dict[Any, Any]:
    return {
        vol.Required(CONF_NAME, default=name): TextSelector(),
        vol.Required(CONF_LEAD_MIN, default=lead_min): NumberSelector(
            NumberSelectorConfig(
                min=0, max=30, step=1, mode=NumberSelectorMode.SLIDER, unit_of_measurement="min"
            )
        ),
        vol.Required(CONF_INCLUDE_CANCELLED, default=include_cancelled): BooleanSelector(),
    }


class _DepartureSteps:
    """Steps line -> direction -> details, shared by the main and subentry flows."""

    hass: HomeAssistant
    backend: DepartureBackend
    stop_id: str
    stop_name: str
    existing_unique_ids: set[str]
    _board: DepartureBoard | None = None
    _lines: dict[str, Line] | None = None
    _line: Line | None = None
    _groups: dict[str, DirectionGroup] | None = None
    _group: DirectionGroup | None = None

    # Provided by FlowHandler.
    async_show_form: Any
    async_abort: Any

    async def _finish(self, subentry: ConfigSubentryData | None) -> Any:
        raise NotImplementedError

    async def _load_lines(self) -> dict[str, Line]:
        """Lines at the stop ∪ lines seen departing in the next 120 min."""
        lines_res, board_res = await asyncio.gather(
            self.backend.lines_at_stop(self.stop_id),
            self.backend.departures(
                self.stop_id,
                direction_stop_id=None,
                duration_min=DISCOVERY_WINDOW_MIN,
                max_results=300,
            ),
            return_exceptions=True,
        )
        if isinstance(board_res, BaseException) and isinstance(lines_res, BaseException):
            raise board_res
        lines: set[Line] = set()
        if not isinstance(lines_res, BaseException):
            lines.update(lines_res)
        if not isinstance(board_res, BaseException):
            self._board = board_res
            lines.update(d.line for d in board_res.departures)
        return {line_key(ln): ln for ln in sorted(lines, key=line_sort_key) if ln.name}

    async def _line_step(
        self, step_id: str, user_input: dict[str, Any] | None, allow_skip: bool
    ) -> Any:
        if user_input is not None and self._lines is not None:
            choice = user_input.get(CONF_LINE)
            if not choice:
                return await self._finish(None)
            self._line = self._lines[choice]
            self._groups = None
            return await self.async_step_direction()
        if self._lines is None:
            try:
                self._lines = await self._load_lines()
            except BackendError:
                return self.async_abort(reason="cannot_connect")
        if not self._lines and not allow_skip:
            return self.async_abort(reason="no_lines")
        selector = SelectSelector(
            SelectSelectorConfig(
                options=[
                    SelectOptionDict(value=key, label=line_label(line))
                    for key, line in self._lines.items()
                ],
                mode=SelectSelectorMode.DROPDOWN,
            )
        )
        field = vol.Optional(CONF_LINE) if allow_skip else vol.Required(CONF_LINE)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({field: selector}),
            description_placeholders={"stop": self.stop_name},
        )

    async def async_step_direction(self, user_input: dict[str, Any] | None = None) -> Any:
        """Pick a direction, i.e. the next stop after ours."""
        assert self._line is not None
        if user_input is not None and self._groups and CONF_DIRECTION in user_input:
            return await self._choose_group(self._groups[user_input[CONF_DIRECTION]])
        if user_input is not None or self._board is None:
            # First visit without a board, or "try again": fetch fresh departures.
            try:
                self._board = await self.backend.departures(
                    self.stop_id,
                    direction_stop_id=None,
                    duration_min=DISCOVERY_WINDOW_MIN,
                    max_results=300,
                )
            except BackendError:
                return self.async_abort(reason="cannot_connect")
            self._groups = None
        if self._groups is None:
            deps = [d for d in self._board.departures if d.line == self._line]
            groups = await build_direction_groups(self.backend, self.stop_id, self._line, deps)
            self._groups = {g.key: g for g in groups}
        placeholders = {"line": self._line.name, "stop": self.stop_name}
        if not self._groups:
            return self.async_show_form(
                step_id="direction",
                data_schema=vol.Schema({}),
                errors={"base": "no_live_departures"},
                description_placeholders=placeholders,
            )
        if len(self._groups) == 1:
            return await self._choose_group(next(iter(self._groups.values())))
        return self.async_show_form(
            step_id="direction",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DIRECTION): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=k, label=g.label)
                                for k, g in self._groups.items()
                            ],
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders=placeholders,
        )

    async def _choose_group(self, group: DirectionGroup) -> Any:
        assert self._line is not None
        if subentry_unique_id(self._line, group) in self.existing_unique_ids:
            return self.async_abort(reason="already_configured")
        self._group = group
        return await self.async_step_details()

    async def async_step_details(self, user_input: dict[str, Any] | None = None) -> Any:
        """Name, walking time, cancelled departures."""
        line, group = self._line, self._group
        assert line is not None and group is not None
        if user_input is not None:
            data = {
                CONF_LINE_NAME: line.name,
                CONF_PRODUCT: line.product,
                CONF_DIRECTION_STOP_ID: group.next_stop_id,
                CONF_DIRECTION_LABEL: " / ".join(short_headsign(h) for h in group.headsigns),
                CONF_NEXT_STOP_NAME: group.next_stop_name,
                CONF_MATCH_MODE: MATCH_NEXT_STOP if group.next_stop_id else MATCH_HEADSIGN,
                CONF_HEADSIGNS: [] if group.next_stop_id else list(group.headsigns),
                CONF_LEAD_MIN: int(user_input[CONF_LEAD_MIN]),
                CONF_INCLUDE_CANCELLED: user_input[CONF_INCLUDE_CANCELLED],
            }
            return await self._finish(
                ConfigSubentryData(
                    data=data,
                    subentry_type=SUBENTRY_DEPARTURE,
                    title=user_input[CONF_NAME].strip() or line.name,
                    unique_id=subentry_unique_id(line, group),
                )
            )
        default_name = f"{line.name} → {short_headsign(group.headsigns[0])}"
        return self.async_show_form(
            step_id="details",
            data_schema=vol.Schema(_details_schema(default_name, 0, False)),
            description_placeholders={
                "line": line.name,
                "direction": group.label,
                "stop": self.stop_name,
            },
        )


class BvgConfigFlow(ConfigFlow, _DepartureSteps, domain=DOMAIN):
    """Add a stop, optionally with its first line."""

    VERSION = 1

    def __init__(self) -> None:
        self._stops: dict[str, Stop] = {}
        self.existing_unique_ids = set()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> BvgOptionsFlow:
        return BvgOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_DEPARTURE: DepartureSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Search for a stop by name."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self.backend = create_backend(
                async_get_clientsession(self.hass), DEFAULT_BACKEND, get_limiter(self.hass)
            )
            try:
                stops = await self.backend.search_stops(user_input[CONF_QUERY].strip())
            except BackendError:
                errors["base"] = "cannot_connect"
            else:
                if not stops:
                    errors[CONF_QUERY] = "no_stops_found"
                elif len(stops) == 1:
                    return await self._select_stop(stops[0])
                else:
                    self._stops = {s.id: s for s in stops}
                    return await self.async_step_pick_stop()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_QUERY): TextSelector()}),
            errors=errors,
        )

    async def async_step_pick_stop(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose among the search hits."""
        if user_input is not None:
            return await self._select_stop(self._stops[user_input[CONF_STOP]])
        return self.async_show_form(
            step_id="pick_stop",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STOP): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                SelectOptionDict(value=s.id, label=stop_label(s))
                                for s in self._stops.values()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def _select_stop(self, stop: Stop) -> ConfigFlowResult:
        await self.async_set_unique_id(stop.id)
        self._abort_if_unique_id_configured()
        self.stop_id, self.stop_name = stop.id, stop.name
        return await self.async_step_first_line()

    async def async_step_first_line(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """First line, so the flow ends with a working sensor. Optional."""
        result: ConfigFlowResult = await self._line_step("first_line", user_input, True)
        return result

    async def _finish(self, subentry: ConfigSubentryData | None) -> ConfigFlowResult:
        return self.async_create_entry(
            title=self.stop_name,
            data={CONF_STOP_ID: self.stop_id, CONF_STOP_NAME: self.stop_name},
            options={
                CONF_BACKEND: DEFAULT_BACKEND,
                CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL,
                CONF_WINDOW: DEFAULT_WINDOW,
            },
            subentries=[subentry] if subentry else [],
        )


class DepartureSubentryFlow(ConfigSubentryFlow, _DepartureSteps):
    """Add or reconfigure one line + direction at a stop."""

    def _setup_from_entry(self) -> ConfigEntry:
        entry = self._get_entry()
        self.stop_id = entry.data[CONF_STOP_ID]
        self.stop_name = entry.data[CONF_STOP_NAME]
        self.backend = create_backend(
            async_get_clientsession(self.hass),
            entry.options.get(CONF_BACKEND, DEFAULT_BACKEND),
            get_limiter(self.hass),
        )
        self.existing_unique_ids = {s.unique_id for s in entry.subentries.values() if s.unique_id}
        return entry

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Entry point of "Add departure"."""
        self._setup_from_entry()
        return await self.async_step_line()

    async def async_step_line(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick the line."""
        result: SubentryFlowResult = await self._line_step("line", user_input, False)
        return result

    async def _finish(self, subentry: ConfigSubentryData | None) -> SubentryFlowResult:
        assert subentry is not None
        return self.async_create_entry(
            title=subentry["title"], data=subentry["data"], unique_id=subentry["unique_id"]
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Edit name, walking time, cancelled and (advanced) headsign matching.

        Line and direction are fixed: delete and re-add to change them.
        """
        entry = self._setup_from_entry()
        subentry = self._get_reconfigure_subentry()
        data = dict(subentry.data)
        errors: dict[str, str] = {}

        if user_input is not None:
            advanced = user_input.get(CONF_ADVANCED, {})
            by_headsign = bool(advanced.get(CONF_MATCH_BY_HEADSIGN))
            headsigns = list(advanced.get(CONF_HEADSIGNS, []))
            if by_headsign and not headsigns:
                errors["base"] = "headsigns_required"
            elif not by_headsign and not data.get(CONF_DIRECTION_STOP_ID):
                errors["base"] = "headsign_only"
            else:
                data.update(
                    {
                        CONF_LEAD_MIN: int(user_input[CONF_LEAD_MIN]),
                        CONF_INCLUDE_CANCELLED: user_input[CONF_INCLUDE_CANCELLED],
                        CONF_MATCH_MODE: MATCH_HEADSIGN if by_headsign else MATCH_NEXT_STOP,
                        CONF_HEADSIGNS: headsigns if by_headsign else [],
                    }
                )
                return self.async_update_and_abort(
                    entry,
                    subentry,
                    title=user_input[CONF_NAME].strip() or subentry.title,
                    data=data,
                )

        seen: dict[str, None] = dict.fromkeys(data.get(CONF_HEADSIGNS) or [])
        try:
            board = await self.backend.departures(
                self.stop_id,
                direction_stop_id=None,
                duration_min=DISCOVERY_WINDOW_MIN,
                max_results=300,
            )
        except BackendError:
            _LOGGER.debug("Could not fetch headsigns for reconfigure")
        else:
            line = Line(data[CONF_LINE_NAME], data[CONF_PRODUCT])
            seen.update(dict.fromkeys(d.headsign for d in board.departures if d.line == line))

        schema = _details_schema(
            subentry.title, data.get(CONF_LEAD_MIN, 0), data.get(CONF_INCLUDE_CANCELLED, False)
        )
        schema[vol.Required(CONF_ADVANCED)] = section(
            vol.Schema(
                {
                    vol.Required(
                        CONF_MATCH_BY_HEADSIGN,
                        default=data.get(CONF_MATCH_MODE) == MATCH_HEADSIGN,
                    ): BooleanSelector(),
                    vol.Optional(
                        CONF_HEADSIGNS, default=list(data.get(CONF_HEADSIGNS) or [])
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=sorted(seen),
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            {"collapsed": data.get(CONF_MATCH_MODE) != MATCH_HEADSIGN},
        )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={
                "line": data[CONF_LINE_NAME],
                "direction": data.get(CONF_DIRECTION_LABEL) or "",
                "stop": self.stop_name,
            },
        )


class BvgOptionsFlow(OptionsFlow):
    """Backend, polling interval, lookahead window."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_BACKEND: user_input[CONF_BACKEND],
                    CONF_UPDATE_INTERVAL: int(user_input[CONF_UPDATE_INTERVAL]),
                    CONF_WINDOW: int(user_input[CONF_WINDOW]),
                }
            )
        opts = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BACKEND, default=opts.get(CONF_BACKEND, DEFAULT_BACKEND)
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=list(BACKENDS),
                            translation_key=CONF_BACKEND,
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    ),
                    vol.Required(
                        CONF_UPDATE_INTERVAL,
                        default=opts.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=30,
                            max=600,
                            step=10,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="s",
                        )
                    ),
                    vol.Required(
                        CONF_WINDOW, default=opts.get(CONF_WINDOW, DEFAULT_WINDOW)
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=30,
                            max=240,
                            step=10,
                            mode=NumberSelectorMode.SLIDER,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
        )
