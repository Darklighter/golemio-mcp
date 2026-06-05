# golemio-mcp

Read-only [MCP](https://modelcontextprotocol.io) server exposing the Prague **Golemio**
open-data API ([api.golemio.cz](https://api.golemio.cz)): real-time PID public-transport
departures, stop search, and a range of city datasets (air quality, parking, waste,
bicycle counters, medical institutions, libraries, playgrounds, gardens, districts).

Built for the host-native **Hermes** agent (`roles/hermes_golemio` in the private-servers
IaC), mirroring the `gramps-mcp` conventions: a Python/`httpx` **stdio** child of the
gateway. Because `httpx` honours `HTTP_PROXY`/`HTTPS_PROXY` natively, egress to
`api.golemio.cz` routes through the orchestrator's Squid allowlist with no shim.

## Tools (all read-only)

| Tool | Purpose |
|------|---------|
| `stop_search(name, limit)` | PID stops by name (diacritics-tolerant) |
| `nearest_stops(lat, lon, range, limit)` | Nearest stops to a coordinate (Telegram pin) |
| `stop_departures(stop \| lat+lon, count, minutes_after)` | Real-time departure board |
| `air_quality(latlng, range, limit)` | Air-quality stations |
| `parking_lots(latlng, range, limit)` | Parking (incl. P+R) with free places |
| `waste_stations(latlng, range, limit)` | Sorted-waste containers |
| `bicycle_counters(latlng, range, limit)` | Bicycle counters |
| `shared_cars(latlng, range, limit)` | Car-sharing vehicles near a point |
| `medical_institutions(latlng, range, limit)` | Hospitals/clinics/pharmacies |
| `municipal_libraries(latlng, range, limit)` | Public libraries |
| `playgrounds(latlng, range, limit)` | Children's playgrounds |
| `gardens(latlng, range, limit)` | Public gardens/parks |
| `city_districts(latlng, range, limit)` | District boundaries/metadata |

Most city datasets accept `latlng="lat,lon"` + `range` (metres) → "what's near a pin",
and results are returned **compact** (key fields + coords + `distance_m`), not raw GeoJSON.

## Config (env)

| Var | Required | Default |
|-----|----------|---------|
| `GOLEMIO_API_KEY` | for city datasets + departures | — (free key: <https://api.golemio.cz/api-keys/>) |
| `GOLEMIO_BASE` | no | `https://api.golemio.cz` |
| `PID_STOPS_URL` | no | `https://data.pid.cz/stops/json/stops.json` (keyless) |
| `GOLEMIO_TIMEOUT` | no | `20` (seconds) |

## Run

Requires [uv](https://docs.astral.sh/uv/). Entrypoint is `python -m src.golemio_mcp.server`
run **from the repo root** (src-layout):

```bash
uv sync                                    # build .venv from uv.lock
GOLEMIO_API_KEY=... uv run --no-sync python -m src.golemio_mcp.server stdio
```

> **First-time setup:** generate the lockfile with `uv lock` and commit `uv.lock` — the
> Hermes role builds with `uv sync --frozen`, which requires it.

MCP client config:

```json
{ "mcpServers": { "golemio": {
  "command": "uv",
  "args": ["run", "--no-sync", "python", "-m", "src.golemio_mcp.server", "stdio"],
  "env": { "GOLEMIO_API_KEY": "your-key" }
} } }
```

## Notes / caveats

* City-dataset endpoints/fields follow the Golemio **output-gateway OpenAPI**
  (`/docs/static/output-gateway/openapi.json`): e.g. `parking` is `/v2/parking`, and nested
  property objects (`company`, `fuel`, `availability`, `address`, `type`, `measurement`,
  `image`) are flattened to a summary scalar by `_scalarize` in `server.py`.
* The **transit** tools target the PID gateway (separate `vp-output-gateway` spec). The PID
  API has **no geo-radius** for stops/departures, so `stop_search`/`nearest_stops` resolve
  from the public static stop register `https://data.pid.cz/stops/json/stops.json`
  (diacritics/partial match + nearest-by-`avgLat/avgLon`; cached in-process), and
  `stop_departures` reads `/v2/pid/departureboards?ids=<gtfs ids>`. **Deploying the transit
  tools requires `data.pid.cz` in the egress allowlist** (in addition to `api.golemio.cz`).
  `GOLEMIO_API_KEY` is needed only for the departure board; stop resolution is keyless.
* All tools are GET-only by construction → there is no write/destructive surface.

## Licence

MIT
