"""transport.rest backend, rate limiter and failover against recorded responses."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import aiohttp
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bvg_departures.api import (
    BACKEND_MGATE,
    BACKEND_TRANSPORT_REST,
    FailoverBackend,
    create_backend,
)
from custom_components.bvg_departures.api.base import (
    BackendError,
    BackendUnavailable,
    ParseError,
    RateLimited,
)
from custom_components.bvg_departures.api.hafas_mgate import MgateBackend
from custom_components.bvg_departures.api.models import Line, normalize_line_name
from custom_components.bvg_departures.api.ratelimit import TokenBucket
from custom_components.bvg_departures.api.transport_rest import BASE_URL, TransportRestBackend
from custom_components.bvg_departures.config_flow import stop_label

from .conftest import TAUERNALLEE, load

ANY_LOCATIONS = re.compile(rf"^{BASE_URL}/locations")
ANY_DEPARTURES = re.compile(rf"^{BASE_URL}/stops/\d+/departures")
ANY_TRIP = re.compile(rf"^{BASE_URL}/trips/")


@pytest.fixture
async def session(aioclient_mock: AiohttpClientMocker) -> Any:
    s = aioclient_mock.create_session(asyncio.get_running_loop())
    yield s
    await s.close()


@pytest.fixture
def backend(session: aiohttp.ClientSession) -> TransportRestBackend:
    return TransportRestBackend(session)


async def test_search_multiple(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_LOCATIONS, json=load("transport_rest/locations_tauernallee.json"))
    stops = await backend.search_stops("Tauernallee")
    url = aioclient_mock.mock_calls[0][1]
    assert url.query["query"] == "Tauernallee"
    assert url.query["linesOfStops"] == "true"
    assert stops[0].id == TAUERNALLEE
    assert stops[0].name == "Tauernallee/Säntisstr."
    assert stops[0].products == frozenset({"bus"})
    assert stop_label(stops[0]) == "Tauernallee/Säntisstr. · M76 N81 X71 X76 179"
    # S Prenzlauer Allee lists S41 four times; lines are de-duplicated.
    assert stops[2].lines.count("S41") == 1
    assert stop_label(stops[2]).endswith("…")


@pytest.mark.parametrize(
    ("fixture", "count"), [("locations_single.json", 1), ("locations_none.json", 0)]
)
async def test_search_single_and_none(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker, fixture: str, count: int
) -> None:
    aioclient_mock.get(ANY_LOCATIONS, json=load(f"transport_rest/{fixture}"))
    assert len(await backend.search_stops("x")) == count


async def test_lines_at_stop(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(
        f"{BASE_URL}/stops/900070301", json=load("transport_rest/stop_alt_mariendorf.json")
    )
    lines = await backend.lines_at_stop("900070301")
    assert Line("U6", "subway") in lines
    assert Line("M76", "bus") in lines


async def _board(backend: TransportRestBackend, direction: str | None = None) -> Any:
    return await backend.departures(
        TAUERNALLEE, direction_stop_id=direction, duration_min=90, max_results=50
    )


async def test_departures(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_DEPARTURES, json=load("transport_rest/departures_tauernallee.json"))
    board = await _board(backend, "900070452")
    url = aioclient_mock.mock_calls[0][1]
    assert url.query["direction"] == "900070452"
    assert url.query["duration"] == "90"
    assert board.backend == "transport_rest"
    assert len(board.departures) == 77
    assert board.realtime_updated is not None
    first = board.departures[0]
    assert first.line == Line("X76", "bus")
    assert first.headsign == "Lichtenrade, Nahariyastr."
    assert first.when is not None and first.when.utcoffset() is not None
    assert first.stop_id == TAUERNALLEE


async def test_departures_unfiltered_has_no_direction(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_DEPARTURES, json=load("transport_rest/departures_empty.json"))
    board = await _board(backend)
    assert "direction" not in aioclient_mock.mock_calls[0][1].query
    assert board.departures == ()
    assert board.realtime_updated is None


async def test_departure_edge_cases(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_DEPARTURES, json=load("transport_rest/departures_edge_cases.json"))
    on_time, delayed, cancelled, no_rt, platform = (await _board(backend)).departures
    assert on_time.delay_s == 0 and on_time.when == on_time.planned
    assert delayed.delay_s == 240
    assert (delayed.effective - delayed.planned).total_seconds() == 240
    assert cancelled.cancelled and cancelled.when is None
    assert cancelled.effective == cancelled.planned
    assert cancelled.remarks == ("Fahrt fällt aus",)
    assert no_rt.delay_s is None
    assert platform.platform == "11" and platform.planned_platform == "12"


async def test_hints_are_not_remarks(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_DEPARTURES, json=load("transport_rest/departures_alt_mariendorf.json"))
    board = await _board(backend)
    all_remarks = {r for d in board.departures for r in d.remarks}
    assert "Bicycle conveyance" not in all_remarks
    assert "Elevator out of service" in all_remarks


@pytest.mark.parametrize(
    ("fixture", "after", "expected"),
    [
        # Bus trips list the bay, not the station.
        ("trip_m76_lichtenrade", "900070702", "900070305"),
        ("trip_m76_wsp", "900070702", "900070306"),
        ("trip_tauern_x76_nahariya", TAUERNALLEE, "900074101"),
        # Ringbahn passes Südkreuz twice; both give the same next stop.
        ("trip_s41", "900058101", "900054104"),
        ("trip_s42", "900058101", "900068201"),
    ],
)
async def test_trip_next_stop(
    backend: TransportRestBackend,
    aioclient_mock: AiohttpClientMocker,
    fixture: str,
    after: str,
    expected: str,
) -> None:
    aioclient_mock.get(ANY_TRIP, json=load(f"transport_rest/{fixture}.json"))
    stop = await backend.trip_next_stop("1|2|3", after)
    assert stop is not None and stop.id == expected
    assert "(Berlin)" not in stop.name
    assert str(aioclient_mock.mock_calls[0][1]).startswith(f"{BASE_URL}/trips/1%7C2%7C3")


async def test_trip_next_stop_terminal_or_missing(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    trip = load("transport_rest/trip_tauern_x76_nahariya.json")
    aioclient_mock.get(ANY_TRIP, json=trip)
    assert await backend.trip_next_stop("x", trip["trip"]["stopovers"][-1]["stop"]["id"]) is None
    assert await backend.trip_next_stop("x", "0") is None


async def test_trip_next_stop_station_match(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    trip = {
        "trip": {
            "stopovers": [
                {"stop": {"id": "1", "name": "Bay", "station": {"id": "900"}}},
                {"stop": {"id": "2", "name": "Next (Berlin)"}},
            ]
        }
    }
    aioclient_mock.get(ANY_TRIP, json=trip)
    stop = await backend.trip_next_stop("x", "900")
    assert stop is not None and stop.name == "Next"


@pytest.mark.parametrize(
    ("header", "expected"),
    [("30", 30), ("Wed, 21 Oct 2015 07:28:00 GMT", None), (None, None)],
)
async def test_429_retry_after(
    backend: TransportRestBackend,
    aioclient_mock: AiohttpClientMocker,
    header: str | None,
    expected: float | None,
) -> None:
    aioclient_mock.get(
        ANY_DEPARTURES, status=429, headers={"Retry-After": header} if header else {}
    )
    with pytest.raises(RateLimited) as err:
        await _board(backend)
    assert err.value.retry_after == expected


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"status": 503}, BackendUnavailable),
        ({"status": 404}, BackendError),
        ({"text": "<html>oops"}, ParseError),
        ({"json": {"nope": []}}, ParseError),
        ({"exc": TimeoutError()}, BackendUnavailable),
        ({"exc": aiohttp.ClientConnectionError()}, BackendUnavailable),
    ],
)
async def test_errors(
    backend: TransportRestBackend,
    aioclient_mock: AiohttpClientMocker,
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    aioclient_mock.get(ANY_DEPARTURES, **kwargs)
    with pytest.raises(error):
        await _board(backend)


async def test_parse_errors_everywhere(
    backend: TransportRestBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(ANY_LOCATIONS, json=[{"type": "stop"}, 3])
    aioclient_mock.get(f"{BASE_URL}/stops/1", json=[])
    aioclient_mock.get(ANY_TRIP, json={"trip": {}})
    with pytest.raises(ParseError):
        await backend.search_stops("x")
    with pytest.raises(ParseError):
        await backend.lines_at_stop("1")
    with pytest.raises(ParseError):
        await backend.trip_next_stop("x", "1")


async def test_limiter_is_used(aioclient_mock: AiohttpClientMocker) -> None:
    calls = []

    class Spy(TokenBucket):
        async def acquire(self) -> None:
            calls.append(1)

    session = aioclient_mock.create_session(asyncio.get_running_loop())
    aioclient_mock.get(ANY_LOCATIONS, json=[])
    await TransportRestBackend(session, Spy()).search_stops("x")
    await session.close()
    assert calls == [1]


def test_normalize_line_name() -> None:
    assert normalize_line_name(" Bus  m76 ") == "M76"
    assert normalize_line_name("ICE  509") == "ICE 509"


async def test_token_bucket_queues() -> None:
    bucket = TokenBucket(rate=2, period=0.2)
    await bucket.acquire()
    await bucket.acquire()
    loop = asyncio.get_running_loop()
    start = loop.time()
    waiter = asyncio.ensure_future(bucket.acquire())
    await asyncio.sleep(0)
    assert not waiter.done()  # queued, not dropped
    await asyncio.wait_for(waiter, 2)
    assert loop.time() - start >= 0.09  # one token refills after period / rate


class _Stub:
    def __init__(self, name: str, error: BackendError | None = None) -> None:
        self.name = name
        self.error = error
        self.calls = 0

    async def _go(self) -> Any:
        self.calls += 1
        if self.error:
            raise self.error
        return self.name

    async def search_stops(self, query: str, limit: int = 8) -> Any:
        return await self._go()

    async def lines_at_stop(self, stop_id: str) -> Any:
        return await self._go()

    async def departures(self, stop_id: str, **kwargs: Any) -> Any:
        return await self._go()

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Any:
        return await self._go()


async def test_failover() -> None:
    primary, fallback = _Stub("a", BackendUnavailable("down")), _Stub("b")
    fo = FailoverBackend(primary, fallback)  # type: ignore[arg-type]
    assert await fo.search_stops("x") == "b"
    assert await fo.lines_at_stop("x") == "b"
    assert await fo.departures("x", direction_stop_id=None, duration_min=1, max_results=1) == "b"
    assert await fo.trip_next_stop("x", "y") == "b"
    assert fo.failovers == 4
    primary.error = None
    assert await fo.search_stops("x") == "a"
    assert fallback.calls == 4


async def test_failover_both_down_keeps_retry_after() -> None:
    fo = FailoverBackend(_Stub("a", RateLimited(42)), _Stub("b", BackendUnavailable("x")))  # type: ignore[arg-type]
    with pytest.raises(RateLimited) as err:
        await fo.search_stops("x")
    assert err.value.retry_after == 42
    fo = FailoverBackend(_Stub("a", ParseError("x")), _Stub("b", BackendUnavailable("y")))  # type: ignore[arg-type]
    with pytest.raises(BackendUnavailable):
        await fo.search_stops("x")


async def test_create_backend(session: aiohttp.ClientSession) -> None:
    limiter = TokenBucket()
    assert isinstance(
        create_backend(session, BACKEND_TRANSPORT_REST, limiter), TransportRestBackend
    )
    assert isinstance(create_backend(session, BACKEND_MGATE, limiter), MgateBackend)
    auto = create_backend(session, "auto", limiter)
    assert isinstance(auto, FailoverBackend)
    assert isinstance(auto.primary, TransportRestBackend)
    assert isinstance(auto.fallback, MgateBackend)
