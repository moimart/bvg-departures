# Test fixtures

## `transport_rest/`

Recorded from `https://v6.bvg.transport.rest` on 2026-10-27 around 13:05 CET,
trimmed to the fields the integration reads.

- `locations_*.json`: stop search. `locations_tauernallee.json` (several hits) is
  real; `locations_single.json` keeps its first hit; `locations_none.json` is
  empty. The live API is fuzzy and almost never returns 0 or 1 hits.
- `departures_tauernallee.json`: unfiltered board, Tauernallee/Säntisstr., 120 min.
- `departures_alt_mariendorf.json`: U Alt-Mariendorf, M76 and U6 only, with
  remarks (hints and warnings).
- `departures_suedkreuz.json`: S Südkreuz, Ringbahn S41/S42 only.
- `departures_edge_cases.json`: on-time, delayed, cancelled, no-realtime and
  platform-change departures, **derived from real departures above**. Nothing
  was cancelled or platform-changed at recording time.
- `trip_*.json`: `/trips/{id}?stopovers=true` for the lines and directions used
  above, trimmed to stop ids/names.
- `tauernallee_next_stops.json`: `line|headsign` → next stop after Tauernallee,
  resolved live from those trips. The fake backend uses it to emulate the
  server-side `direction=` filter.

## `mgate/`

Real BVG/VBB HAFAS mgate `res` payloads from
[hafas-client](https://github.com/public-transport/hafas-client) (ISC licence)
`test/fixtures/` at commit `5fe8fbd35e1bbcdd5478e425e639fa07668b71eb`:

- `stationboard_vbb.json` ← `vbb-departures.json` (StationBoard, DEP)
- `stationboard_bvg_arrivals.json` ← `bvg-arrivals.json` (StationBoard, ARR)
- `journeydetails_bvg.json` ← `bvg-trip-with-occupancy.json` (JourneyDetails)

`locmatch_synthetic.json` is **synthetic**: hafas-client records no LocMatch
response. It uses the documented `res.match.locL` shape with stop and product
entries copied from `stationboard_bvg_arrivals.json`.

The hafas-client licence (ISC) is in `mgate/LICENSE-hafas-client.md`.
