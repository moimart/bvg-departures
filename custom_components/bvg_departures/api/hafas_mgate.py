"""Fallback backend: BVG's HAFAS mgate endpoint, spoken directly.

Implements only LocMatch, StationBoard and JourneyDetails. The response
parsing mirrors hafas-client's `parse/` modules (see hafas_profile.py for the
commit), so results match what transport.rest returns for the same data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
import re
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from . import hafas_profile as profile
from .base import BackendError, ParseError, fetch_json
from .models import (
    Departure,
    DepartureBoard,
    Line,
    Stop,
    clean_stop_name,
    normalize_line_name,
)
from .ratelimit import TokenBucket

NAME = "mgate"
_TZ = ZoneInfo(profile.TIMEZONE)
_PRODUCT_BY_BIT = {bit: pid for pid, bit in profile.PRODUCT_BITMASKS.items()}
_RING_CW = re.compile(r"^ringbahn s\s?41$", re.IGNORECASE)
_RING_CCW = re.compile(r"^ringbahn s\s?42$", re.IGNORECASE)


def _lid(stop_id: str) -> dict[str, str]:
    return {"type": "S", "lid": f"A=1@L={stop_id}@"}


def _products_from_bitmask(bitmask: int) -> frozenset[str]:
    return frozenset(pid for pid, bit in profile.PRODUCT_BITMASKS.items() if bitmask & bit)


def parse_datetime(date: str, time: str, tz_offset_min: int | None = None) -> datetime:
    """Parse HAFAS `YYYYMMDD` + `[DD]HHMMSS` (DD = day offset)."""
    days = int(time[:-6]) if len(time) > 6 else 0
    naive = datetime(
        int(date[-8:-4]),
        int(date[-4:-2]),
        int(date[-2:]),
        int(time[-6:-4]),
        int(time[-4:-2]),
        int(time[-2:]),
    )
    tz = timezone(timedelta(minutes=tz_offset_min)) if tz_offset_min is not None else _TZ
    return naive.replace(tzinfo=tz) + timedelta(days=days)


def _line(prod: dict[str, Any] | None) -> Line:
    prod = prod or {}
    ctx = prod.get("prodCtx") or {}
    name = ctx.get("line") or prod.get("line") or prod.get("addName") or prod.get("name") or ""
    product = _PRODUCT_BY_BIT.get(int(prod.get("cls", 0)), "")
    return Line(normalize_line_name(name), product)


def _stop(loc: dict[str, Any], prod_l: list[dict[str, Any]]) -> Stop:
    lines: dict[str, None] = {}
    for idx in loc.get("pRefL") or []:
        if 0 <= idx < len(prod_l):
            name = _line(prod_l[idx]).name
            if name:
                lines[name] = None
    return Stop(
        id=str(loc.get("extId") or ""),
        name=clean_stop_name(loc.get("name") or ""),
        products=_products_from_bitmask(int(loc.get("pCls") or 0)),
        lines=tuple(lines),
    )


def _headsign(dir_txt: str | None, line: Line) -> str:
    text = (dir_txt or "").strip()
    if line.product == "suburban":
        if _RING_CW.match(text):
            return "Ringbahn S41 ⟳"
        if _RING_CCW.match(text):
            return "Ringbahn S42 ⟲"
    return text


def _warnings(jny: dict[str, Any], him_l: list[dict[str, Any]]) -> tuple[str, ...]:
    msgs = [*(jny.get("msgL") or []), *((jny.get("stbStop") or {}).get("msgL") or [])]
    out: dict[str, None] = {}
    for msg in msgs:
        if msg.get("type") != "HIM":
            continue
        idx = msg.get("himX")
        if isinstance(idx, int) and 0 <= idx < len(him_l):
            him = him_l[idx]
            text = (him.get("head") or him.get("text") or "").strip()
            if text:
                out[text] = None
    return tuple(out)


def parse_departure(jny: dict[str, Any], common: dict[str, Any]) -> Departure:
    """Parse one StationBoard `jnyL` item."""
    prod_l = common.get("prodL") or []
    loc_l = common.get("locL") or []
    stb = jny["stbStop"]
    prod_x = stb.get("dProdX", jny.get("prodX"))
    line = _line(prod_l[prod_x] if isinstance(prod_x, int) and prod_x < len(prod_l) else None)
    tz = stb.get("dTZOffset")
    planned = parse_datetime(jny["date"], stb["dTimeS"], tz)
    realtime = parse_datetime(jny["date"], stb["dTimeR"], tz) if stb.get("dTimeR") else None
    cancelled = bool(stb.get("dCncl"))
    planned_pl = stb.get("dPlatfS") or (stb.get("dPltfS") or {}).get("txt")
    realtime_pl = stb.get("dPlatfR") or (stb.get("dPltfR") or {}).get("txt")
    loc_x = stb.get("locX")
    stop_id = (
        str(loc_l[loc_x].get("extId")) if isinstance(loc_x, int) and loc_x < len(loc_l) else None
    )
    return Departure(
        trip_id=jny["jid"],
        line=line,
        headsign=_headsign(jny.get("dirTxt"), line),
        when=None if cancelled else (realtime or planned),
        planned=planned,
        delay_s=int((realtime - planned).total_seconds()) if realtime else None,
        platform=None if cancelled else (realtime_pl or planned_pl),
        planned_platform=planned_pl,
        cancelled=cancelled,
        remarks=_warnings(jny, common.get("himL") or []),
        stop_id=stop_id,
    )


class MgateBackend:
    """Minimal HAFAS mgate client for BVG."""

    name = NAME

    def __init__(
        self,
        session: aiohttp.ClientSession,
        limiter: TokenBucket | None = None,
        endpoint: str = profile.ENDPOINT,
    ) -> None:
        self._session = session
        self._limiter = limiter
        self._endpoint = endpoint

    async def _call(self, meth: str, req: dict[str, Any]) -> dict[str, Any]:
        body = {
            "lang": profile.LANG,
            "svcReqL": [{"meth": meth, "req": req}],
            "client": profile.CLIENT,
            "ext": profile.EXT,
            "ver": profile.VER,
            "auth": profile.AUTH,
        }
        data = await fetch_json(
            self._session, "POST", self._endpoint, json_body=body, limiter=self._limiter
        )
        if not isinstance(data, dict):
            raise ParseError("mgate response is not an object")
        if data.get("err") not in (None, "OK"):
            raise BackendError(f"mgate error {data.get('err')}: {data.get('errTxt')}")
        try:
            svc = data["svcResL"][0]
        except (KeyError, IndexError, TypeError) as err:
            raise ParseError("mgate response without svcResL") from err
        if svc.get("err") != "OK":
            raise BackendError(f"mgate {meth} error {svc.get('err')}: {svc.get('errTxt')}")
        res = svc.get("res")
        if not isinstance(res, dict):
            raise ParseError(f"mgate {meth} without res")
        return res

    async def search_stops(self, query: str, limit: int = 8) -> list[Stop]:
        res = await self._call(
            "LocMatch",
            {"input": {"loc": {"type": "S", "name": query + "?"}, "maxLoc": limit, "field": "S"}},
        )
        try:
            prod_l = (res.get("common") or {}).get("prodL") or []
            return [
                _stop(loc, prod_l)
                for loc in res["match"].get("locL") or []
                if loc.get("type") == "S" and loc.get("extId")
            ]
        except (KeyError, TypeError, AttributeError, ValueError) as err:
            raise ParseError("unexpected LocMatch response") from err

    async def _board(
        self,
        stop_id: str,
        direction_stop_id: str | None,
        duration_min: int,
        max_results: int,
    ) -> dict[str, Any]:
        now = datetime.now(_TZ)
        req: dict[str, Any] = {
            "type": "DEP",
            "date": now.strftime("%Y%m%d"),
            "time": now.strftime("%H%M%S"),
            "stbLoc": _lid(stop_id),
            "dur": duration_min,
            "maxJny": max_results,
            "getPasslist": False,
        }
        if direction_stop_id:
            req["dirLoc"] = _lid(direction_stop_id)
        return await self._call("StationBoard", req)

    async def lines_at_stop(self, stop_id: str) -> list[Line]:
        # StationBoard's common.locL carries the stop with its product refs,
        # which is exactly the "lines of stop" list; no LocDetails needed.
        res = await self._board(stop_id, None, 120, 300)
        try:
            common = res.get("common") or {}
            prod_l = common.get("prodL") or []
            seen: dict[tuple[str, str], Line] = {}
            for loc in common.get("locL") or []:
                if str(loc.get("extId")) == stop_id:
                    for idx in loc.get("pRefL") or []:
                        line = _line(prod_l[idx])
                        if line.name:
                            seen.setdefault((line.product, line.name), line)
            for jny in res.get("jnyL") or []:
                line = parse_departure(jny, common).line
                if line.name:
                    seen.setdefault((line.product, line.name), line)
        except (KeyError, TypeError, AttributeError, ValueError, IndexError) as err:
            raise ParseError("unexpected StationBoard response") from err
        return list(seen.values())

    async def departures(
        self,
        stop_id: str,
        *,
        direction_stop_id: str | None,
        duration_min: int,
        max_results: int,
    ) -> DepartureBoard:
        res = await self._board(stop_id, direction_stop_id, duration_min, max_results)
        try:
            common = res.get("common") or {}
            deps = tuple(parse_departure(j, common) for j in res.get("jnyL") or [])
            planrt = res.get("planrtTS")
        except (KeyError, TypeError, AttributeError, ValueError, IndexError) as err:
            raise ParseError("unexpected StationBoard response") from err
        return DepartureBoard(
            departures=deps,
            realtime_updated=datetime.fromtimestamp(int(planrt), UTC) if planrt else None,
            fetched_at=datetime.now(UTC),
            backend=NAME,
        )

    async def trip_next_stop(self, trip_id: str, after_stop_id: str) -> Stop | None:
        res = await self._call("JourneyDetails", {"jid": trip_id, "getPolyline": False})
        try:
            common = res.get("common") or {}
            loc_l = common.get("locL") or []
            prod_l = common.get("prodL") or []
            stops = [loc_l[s["locX"]] for s in res["journey"]["stopL"]]
        except (KeyError, TypeError, IndexError) as err:
            raise ParseError("unexpected JourneyDetails response") from err
        for i, loc in enumerate(stops[:-1]):
            if str(loc.get("extId")) == after_stop_id:
                return _stop(stops[i + 1], prod_l)
        return None
