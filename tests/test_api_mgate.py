"""HAFAS mgate backend against responses recorded by hafas-client.

hafas-client's fixtures hold the `res` part of a service result; `_wrap` puts it
back into the envelope the endpoint returns. The LocMatch fixture is synthetic
(hafas-client records none); see tests/fixtures/README.md.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.bvg_departures.api import hafas_profile as profile
from custom_components.bvg_departures.api.base import BackendError, ParseError
from custom_components.bvg_departures.api.hafas_mgate import (
    MgateBackend,
    parse_datetime,
    parse_departure,
)
from custom_components.bvg_departures.api.models import Line

from .conftest import load


def _wrap(res: Any, err: str = "OK") -> dict[str, Any]:
    return {"ver": profile.VER, "err": "OK", "svcResL": [{"meth": "X", "err": err, "res": res}]}


@pytest.fixture
async def backend(aioclient_mock: AiohttpClientMocker) -> Any:
    session = aioclient_mock.create_session(asyncio.get_running_loop())
    yield MgateBackend(session)
    await session.close()


def _sent(mock: AiohttpClientMocker, index: int = 0) -> dict[str, Any]:
    body: dict[str, Any] = mock.mock_calls[index][2]
    return body


async def test_departures(backend: MgateBackend, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=_wrap(load("mgate/stationboard_vbb.json")))
    board = await backend.departures(
        "900100004", direction_stop_id="900100003", duration_min=30, max_results=20
    )
    body = _sent(aioclient_mock)
    # Request carries the profile copied from hafas-client, and dirLoc.
    assert body["auth"] == profile.AUTH and body["client"] == profile.CLIENT
    assert body["ver"] == profile.VER and body["ext"] == profile.EXT
    req = body["svcReqL"][0]
    assert req["meth"] == "StationBoard"
    assert req["req"]["stbLoc"]["lid"] == "A=1@L=900100004@"
    assert req["req"]["dirLoc"]["lid"] == "A=1@L=900100003@"
    assert req["req"]["type"] == "DEP" and req["req"]["dur"] == 30

    assert board.backend == "mgate"
    assert board.realtime_updated == datetime.fromtimestamp(1634053386, UTC)
    first = board.departures[0]
    assert first.line == Line("S3", "suburban")
    assert first.headsign == "S Erkner Bhf"
    assert first.trip_id == "1|24934|5|86|12102021"
    assert first.planned.isoformat() == "2021-10-12T17:44:00+02:00"
    assert first.delay_s == 0
    assert first.platform == "3"
    assert first.stop_id == "900100004"
    assert {d.line.product for d in board.departures} >= {"suburban", "bus", "subway"}


async def test_departures_without_direction(
    backend: MgateBackend, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=_wrap({"jnyL": []}))
    board = await backend.departures("1", direction_stop_id=None, duration_min=5, max_results=5)
    assert "dirLoc" not in _sent(aioclient_mock)["svcReqL"][0]["req"]
    assert board.departures == () and board.realtime_updated is None


async def test_lines_at_stop(backend: MgateBackend, aioclient_mock: AiohttpClientMocker) -> None:
    # The recorded board is an arrivals board; keep its `common` (which lists
    # the stop's product refs) and add departures from the VBB board.
    res = load("mgate/stationboard_bvg_arrivals.json")
    vbb = load("mgate/stationboard_vbb.json")
    res["jnyL"] = []
    aioclient_mock.post(profile.ENDPOINT, json=_wrap(res))
    lines = await backend.lines_at_stop("900110004")
    assert {Line("S41", "suburban"), Line("S42", "suburban"), Line("S8", "suburban")} <= set(lines)
    # Same name under another product (e.g. a replacement bus) is a distinct line.
    assert len(lines) == len(set(lines))

    aioclient_mock.clear_requests()
    aioclient_mock.post(profile.ENDPOINT, json=_wrap(vbb))
    seen = {line.name for line in await backend.lines_at_stop("900100004")}
    assert {"S3", "U8", "300"} <= seen


async def test_trip_next_stop(backend: MgateBackend, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=_wrap(load("mgate/journeydetails_bvg.json")))
    stop = await backend.trip_next_stop("1|x", "900009102")
    assert stop is not None and stop.id == "900009151"
    assert await backend.trip_next_stop("1|x", "nope") is None
    assert _sent(aioclient_mock)["svcReqL"][0] == {
        "meth": "JourneyDetails",
        "req": {"jid": "1|x", "getPolyline": False},
    }


async def test_search(backend: MgateBackend, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=_wrap(load("mgate/locmatch_synthetic.json")))
    stops = await backend.search_stops("Landsberger")
    assert _sent(aioclient_mock)["svcReqL"][0]["req"]["input"]["loc"]["name"] == "Landsberger?"
    assert stops[0].id == "900110004"
    assert stops[0].name == "S Landsberger Allee"
    assert "suburban" in stops[0].products
    assert "S41" in stops[0].lines


def test_parse_datetime() -> None:
    assert parse_datetime("20261027", "130400").isoformat() == "2026-10-27T13:04:00+01:00"
    # Day-offset prefix and explicit tz offset
    assert parse_datetime("20261027", "01003000", 120).isoformat() == "2026-10-28T00:30:00+02:00"
    # Summer date gets CEST from the zone
    assert parse_datetime("20260701", "120000").utcoffset() == timedelta(hours=2)


def test_ringbahn_and_cancelled() -> None:
    common = {
        "prodL": [{"name": "S41", "cls": 1, "prodCtx": {"line": "S41"}}],
        "locL": [{"extId": "900058101"}],
        "himL": [{"head": "Bauarbeiten"}],
    }
    jny: dict[str, Any] = {
        "jid": "1|1",
        "date": "20261027",
        "prodX": 0,
        "dirTxt": "Ringbahn S 41",
        "msgL": [{"type": "HIM", "himX": 0}, {"type": "REM", "remX": 0}],
        "stbStop": {
            "locX": 0,
            "dTimeS": "130000",
            "dTimeR": "130300",
            "dCncl": True,
            "dPltfS": {"txt": "4"},
        },
    }
    dep = parse_departure(jny, common)
    assert dep.headsign == "Ringbahn S41 ⟳"
    assert dep.cancelled and dep.when is None and dep.platform is None
    assert dep.planned_platform == "4" and dep.delay_s == 180
    assert dep.remarks == ("Bauarbeiten",)
    jny["dirTxt"] = "Ringbahn S42"
    assert parse_departure(jny, common).headsign == "Ringbahn S42 ⟲"


@pytest.mark.parametrize(
    "payload", [{"err": "AUTH", "errTxt": "bad aid"}, _wrap({}, err="LOCATION")]
)
async def test_hafas_errors(
    backend: MgateBackend, aioclient_mock: AiohttpClientMocker, payload: dict[str, Any]
) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=payload)
    with pytest.raises(BackendError):
        await backend.search_stops("x")


@pytest.mark.parametrize(
    ("payload", "call"),
    [
        ([1], "search"),
        ({"svcResL": []}, "search"),
        ({"svcResL": [{"err": "OK"}]}, "search"),
        (_wrap({"match": None}), "search"),
        (_wrap({"jnyL": [{"stbStop": {}}]}), "departures"),
        (_wrap({"jnyL": [{"stbStop": {}}]}), "lines"),
        (_wrap({"journey": {}}), "trip"),
    ],
)
async def test_parse_errors(
    backend: MgateBackend, aioclient_mock: AiohttpClientMocker, payload: Any, call: str
) -> None:
    aioclient_mock.post(profile.ENDPOINT, json=payload)
    with pytest.raises(ParseError):
        if call == "search":
            await backend.search_stops("x")
        elif call == "departures":
            await backend.departures("1", direction_stop_id=None, duration_min=1, max_results=1)
        elif call == "lines":
            await backend.lines_at_stop("1")
        else:
            await backend.trip_next_stop("1", "2")
