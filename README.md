# BVG Departures

A Home Assistant integration for Berlin public transport (BVG/VBB). It creates
**one sensor per stop + line + direction**, and its state is the **next
departure as a timestamp**, so a tile card shows "in 4 min" with no templates
or YAML.

```
sensor.m76_lichtenrade    2026-10-27T13:13:00+01:00
sensor.x76_nahariyastr    2026-10-27T13:07:00+01:00
```

## Installation (HACS)

1. HACS → ⋮ → *Custom repositories* → add this repository as *Integration*.
2. Install **BVG Departures** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → BVG Departures*.

Requires Home Assistant 2026.9 or newer.

## Setup

Everything is chosen from pickers. You never type a stop ID or a filter.

1. **Stop**: type part of the name and pick it from the list. Each hit shows
   the lines that serve it: `Tauernallee/Säntisstr. · M76 N81 X71 X76 179`.
2. **Line**: pick from the lines at that stop. Leave it empty to add lines later.
3. **Direction**: pick where the line is going. The next stop is shown in
   brackets: `→ Lichtenrade  (Ankogelweg)`.
4. **Details**: name, walking time (departures sooner than this are skipped)
   and whether to show cancelled trips.

Add more lines with **Add departure** on the stop's card. Each one is a
subentry with its own device and entities.

### Example: Tauernallee

| Departure | Direction (next stop) |
|---|---|
| M76 → Alt-Mariendorf / Walther-Schreiber-Platz | Glärnischweg |
| 179 → Alt-Mariendorf | Glärnischweg |
| X71 → Alt-Mariendorf | U Alt-Mariendorf |
| X76 → Alt-Mariendorf | U Alt-Mariendorf |
| M76 → Lichtenrade | Ankogelweg |
| X76 → Lichtenrade, Nahariyastr. | Mariendorfer Damm/Buckower Ch. |

These are six entities, but only four API requests per update: lines that
share a next stop share a request. The express lines X71/X76 skip stops, so
their "next stop" differs from M76/179 even though they head the same way.

## How directions work

Headsigns are unreliable: short-turns ("Alt-Mariendorf" vs. "Lichtenrade"),
variants and renames all break text matching. A direction is therefore **the
stop after yours**. That is stable across short-turns, separates the Ringbahn
⟳/⟲, and is passed to the API as a server-side filter.

If a line splits before the next stop, edit the departure and turn on
**Advanced → Match by headsign**, then tick the headsigns you want.

## Entities

Per departure (device = the departure, under a device for the stop):

| Entity | Default | State |
|---|---|---|
| *M76 → Lichtenrade* | enabled | next departure (timestamp) |
| Minutes until departure | disabled | minutes, recomputed every 30 s without an API call |
| Delay | disabled | minutes |
| Following departure | disabled | timestamp of the one after |

Attributes of the main sensor: `line`, `product`, `headsign`, `planned`,
`delay_min`, `realtime`, `platform`, `platform_changed`, `cancelled`,
`remarks` (warnings only), `upcoming` (next 3), `last_update`, `backend`.
Only `line` and `delay_min` are recorded in history.

Renaming a departure or changing its walking time never changes its entity ID.
To change the line or direction, delete the departure and add it again.

Service: `bvg_departures.refresh` (target entities or devices) fetches now.

## Options (per stop)

- **Data source**: *Automatic* (default), *transport.rest only*, *BVG HAFAS only*.
- **Update interval**: 30–600 s (default 60). When nothing you watch leaves
  within 20 minutes, polling slows to 5 minutes.
- **Lookahead window**: 30–240 min (default 90).

## Data sources and resilience

- **Primary:** [v6.bvg.transport.rest](https://v6.bvg.transport.rest), community-run
  and rate-limited (~100 req/min). All stops share a limit of 60 requests per minute.
- **Fallback:** BVG's HAFAS `mgate` endpoint, spoken directly, with the client
  profile from [hafas-client](https://github.com/public-transport/hafas-client)
  (see `api/hafas_profile.py`).

In *Automatic* mode a failed request (timeout, 5xx, 429, bad JSON) is retried
on the fallback in the same cycle. If both fail, the integration backs off
60 → 120 → 240 → 600 s, or honours `Retry-After`, and keeps serving cached
departures. Departures that have left are dropped every 30 s, so the state
keeps advancing. Entities go unavailable only when the cache is empty, or when
it is older than 15 minutes and has nothing upcoming left. After 15 minutes of
total outage a Repairs issue appears, and it clears itself on recovery.

## Development

```bash
uv venv -p 3.14 && uv pip install -r requirements_test.txt
.venv/bin/pytest --cov=custom_components.bvg_departures
.venv/bin/ruff check . && .venv/bin/mypy
```

Tests use recorded responses; see `tests/fixtures/README.md`.
