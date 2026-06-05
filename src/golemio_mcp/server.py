"""Golemio MCP server.

Read-only MCP exposing the Prague Golemio open-data API (https://api.golemio.cz):
real-time PID departures, stop search, and a range of city datasets (air quality,
parking, waste, bicycle counters, medical institutions, libraries, playgrounds,
gardens, districts).

Design notes (mirrors the Hermes gramps-mcp conventions):
  * Python + httpx → honours HTTP_PROXY/HTTPS_PROXY natively (httpx trust_env), so when
    the Hermes gateway spawns this as a stdio child with those vars set, egress to
    api.golemio.cz routes through the orchestrator Squid allowlist. No global-agent shim.
  * Every tool is READ-ONLY (GET) → "no destructive ops" holds structurally.
  * Tools return COMPACT, already-trimmed JSON (a short list of dicts), not raw GeoJSON,
    so the model is not flooded with the full Golemio payload.
  * Most city datasets accept `latlng` ("lat,lon") + `range` (metres) → "near a pin".

Endpoints verified against MrMebelMan/golemio-mcp-server. Auth header: X-Access-Token.
Entrypoint: `python -m src.golemio_mcp.server stdio` (run from the repo root).
"""

from __future__ import annotations

import math
import os
import re
import sys
import unicodedata
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

GOLEMIO_BASE = os.environ.get("GOLEMIO_BASE", "https://api.golemio.cz").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("GOLEMIO_TIMEOUT", "20"))

# Public static PID stop register (no API key). The Golemio PID API has NO geo-radius
# parameter for stops/departures, so stop search + nearest-stop are resolved from this
# file (it carries avgLat/avgLon + gtfsIds per stop group). Requires `data.pid.cz` in the
# egress allowlist. Cached in-process after the first fetch (~19 MB).
PID_STOPS_URL = os.environ.get("PID_STOPS_URL", "https://data.pid.cz/stops/json/stops.json")
_GTFS_ID_RE = re.compile(r"^U\d+Z\d+")

mcp = FastMCP("golemio")


def _api_key() -> str:
    key = os.environ.get("GOLEMIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GOLEMIO_API_KEY is not set (get a free key at https://api.golemio.cz/api-keys/).")
    return key


async def _get(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    """GET a Golemio endpoint and return the decoded JSON. Strips None params.

    httpx reads HTTP(S)_PROXY from the environment (trust_env, default), so egress
    goes through the Squid allowlist when the gateway sets those vars.
    """
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    headers = {"X-Access-Token": _api_key(), "Accept": "application/json"}
    url = f"{GOLEMIO_BASE}{endpoint}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, params=clean, headers=headers)
        resp.raise_for_status()
        return resp.json()


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Great-circle distance in metres (rounded)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return int(round(2 * r * math.asin(math.sqrt(a))))


def _parse_latlng(latlng: str | None) -> tuple[float, float] | None:
    if not latlng:
        return None
    try:
        lat, lon = (float(x) for x in latlng.split(","))
        return lat, lon
    except (ValueError, TypeError):
        return None


def _features(data: Any) -> list[dict[str, Any]]:
    """Pull a GeoJSON FeatureCollection (or a bare list) into a list of features."""
    if isinstance(data, dict) and isinstance(data.get("features"), list):
        return data["features"]
    if isinstance(data, list):
        return data
    return []


# Golemio feature properties often nest a small object (company, fuel, availability,
# address, type, measurement, image, …). Flatten each to its single human-relevant
# scalar so the output stays compact. Key order = priority; first present wins.
_SCALAR_KEYS = ("address_formatted", "name", "description", "AQ_hourly_index", "url")


def _scalarize(value: Any) -> Any:
    """Collapse a nested object property to a summary scalar (else return as-is)."""
    if isinstance(value, dict):
        for k in _SCALAR_KEYS:
            if value.get(k) not in (None, ""):
                return value[k]
    return value


def _compact_features(
    data: Any,
    fields: list[str],
    origin: tuple[float, float] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Trim GeoJSON features to the named property `fields` + coords (+ distance).

    `fields` are property keys; missing keys are skipped, and nested-object values are
    flattened via _scalarize. If `origin` is given, a `distance_m` is added and results
    are sorted nearest-first.
    """
    out: list[dict[str, Any]] = []
    for feat in _features(data):
        props = (feat.get("properties") if isinstance(feat, dict) else None) or feat or {}
        geom = (feat.get("geometry") if isinstance(feat, dict) else None) or {}
        item: dict[str, Any] = {}
        for f in fields:
            if f in props and props[f] not in (None, ""):
                item[f] = _scalarize(props[f])
        coords = geom.get("coordinates") if isinstance(geom, dict) else None
        # Only a Point ([lon, lat] of numbers) yields a usable coordinate; skip polygons.
        if isinstance(coords, list) and len(coords) >= 2 and all(isinstance(c, (int, float)) for c in coords[:2]):
            lon, lat = coords[0], coords[1]
            item["lat"], item["lon"] = lat, lon
            if origin:
                item["distance_m"] = _haversine_m(origin[0], origin[1], lat, lon)
        out.append(item)
    if origin:
        out.sort(key=lambda x: x.get("distance_m", 1_000_000_000))
    return out[:limit]


# --------------------------------------------------------------------------- #
# Transit: stops (static PID register) + departures (Golemio departureboards)
# --------------------------------------------------------------------------- #

_STOP_GROUPS: list[dict[str, Any]] | None = None


def _norm(text: str) -> str:
    """Lowercase + strip diacritics (so "Mustek" matches "Můstek")."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


async def _stop_groups() -> list[dict[str, Any]]:
    """Load + cache the PID static stop register (list of stop groups)."""
    global _STOP_GROUPS
    if _STOP_GROUPS is None:
        async with httpx.AsyncClient(timeout=max(HTTP_TIMEOUT, 60)) as client:
            resp = await client.get(PID_STOPS_URL)
            resp.raise_for_status()
            _STOP_GROUPS = resp.json().get("stopGroups", [])
    return _STOP_GROUPS


def _group_compact(g: dict[str, Any], origin: tuple[float, float] | None = None) -> dict[str, Any]:
    gtfs_ids = [gid for s in (g.get("stops") or []) for gid in (s.get("gtfsIds") or [])]
    lat, lon = g.get("avgLat"), g.get("avgLon")
    item: dict[str, Any] = {
        "stop_name": g.get("fullName") or g.get("name"),
        "gtfs_ids": gtfs_ids,
        "municipality": g.get("municipality"),
        "lat": lat,
        "lon": lon,
    }
    if origin and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        item["distance_m"] = _haversine_m(origin[0], origin[1], lat, lon)
    return item


@mcp.tool()
async def stop_search(name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search Prague PID public-transport stops by name (diacritics/partial tolerant).

    Returns compact stop-group records: stop_name, gtfs_ids (all platforms), coordinates.
    Pass a returned gtfs_id (or the name) to stop_departures. Example: name="Anděl".
    """
    q = _norm(name)
    groups = await _stop_groups()
    matches = [g for g in groups if q in _norm(g.get("fullName") or g.get("name") or "")]
    # Prefer exact-name hits first, then by name length (closer match), then alphabetical.
    matches.sort(key=lambda g: (_norm(g.get("name") or "") != q, len(g.get("name") or ""), g.get("name") or ""))
    return [_group_compact(g) for g in matches[:limit]]


@mcp.tool()
async def nearest_stops(lat: float, lon: float, range: int = 500, limit: int = 10) -> list[dict[str, Any]]:
    """Nearest PID stops to a coordinate (use for a Telegram location pin).

    `range` is the search radius in metres. Returns compact stop-group records sorted
    nearest-first with distance_m. Feed a returned gtfs_id into stop_departures.
    """
    groups = await _stop_groups()
    out = []
    for g in groups:
        la, lo = g.get("avgLat"), g.get("avgLon")
        if not isinstance(la, (int, float)) or not isinstance(lo, (int, float)):
            continue
        d = _haversine_m(lat, lon, la, lo)
        if d <= range:
            item = _group_compact(g, origin=(lat, lon))
            out.append(item)
    out.sort(key=lambda x: x.get("distance_m", 1_000_000_000))
    return out[:limit]


def _fmt_departure(dep: dict[str, Any]) -> dict[str, Any]:
    route = dep.get("route") or {}
    trip = dep.get("trip") or {}
    dts = dep.get("departure_timestamp") or {}
    delay = dep.get("delay") or {}
    stop = dep.get("stop") or {}
    last = dep.get("last_stop") or {}
    iso = dts.get("predicted") or dts.get("scheduled")
    hhmm = None
    if isinstance(iso, str) and "T" in iso:
        hhmm = iso.split("T", 1)[1][:5]
    return {
        "line": route.get("short_name"),
        "headsign": trip.get("headsign") or last.get("name"),
        "time": hhmm,
        "minutes": dts.get("minutes"),
        "delay_min": delay.get("minutes") if delay.get("is_available") else None,
        "realtime": bool(delay.get("is_available")),
        "platform": stop.get("platform_code"),
        "night": route.get("is_night"),
        "canceled": trip.get("is_canceled"),
    }


async def _resolve_gtfs_ids(
    stop: str | None, lat: float | None, lon: float | None
) -> tuple[list[str], str | None]:
    """Resolve a stop selector to GTFS stop_ids (+ a display name). Raises if nothing fits."""
    if stop and _GTFS_ID_RE.match(stop.strip()):
        return [stop.strip()], stop.strip()
    if stop:
        hits = await stop_search(stop, limit=1)
        if hits:
            return hits[0]["gtfs_ids"], hits[0]["stop_name"]
        raise ValueError(f"No PID stop found matching {stop!r}.")
    if lat is not None and lon is not None:
        near = await nearest_stops(lat, lon, range=600, limit=1)
        if near:
            return near[0]["gtfs_ids"], near[0]["stop_name"]
        raise ValueError("No PID stop near the given coordinates.")
    raise ValueError("Provide `stop` (name or GTFS stop_id) or `lat`+`lon`.")


@mcp.tool()
async def stop_departures(
    stop: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    count: int = 10,
    minutes_after: int = 90,
) -> dict[str, Any]:
    """Next real-time PID departures from a stop (departure board).

    Identify the stop ONE of two ways:
      * `stop` = a stop name (e.g. "Anděl", resolved via stop_search) or a GTFS stop_id,
      * `lat`+`lon` = coordinates (e.g. a Telegram pin) → nearest stop's board.
    Returns the resolved stop name plus a compact, time-sorted list of upcoming departures
    (line, headsign, time HH:MM, minutes-until, delay, realtime flag, platform).
    """
    gtfs_ids, resolved_name = await _resolve_gtfs_ids(stop, lat, lon)
    if not gtfs_ids:
        return {"stop": resolved_name, "departures": []}
    # departureboards takes repeated `ids` params; httpx encodes a list as ids=A&ids=B.
    data = await _get(
        "/v2/pid/departureboards",
        {"ids": gtfs_ids, "minutesAfter": minutes_after, "limit": count, "total": count},
    )
    departures = data.get("departures") if isinstance(data, dict) else None
    stops = data.get("stops") if isinstance(data, dict) else None
    name = resolved_name
    if not name and stops:
        name = next((s.get("stop_name") for s in stops if isinstance(s, dict) and s.get("stop_name")), None)
    return {
        "stop": name,
        "departures": [_fmt_departure(d) for d in (departures or []) if isinstance(d, dict)][:count],
    }


# --------------------------------------------------------------------------- #
# City datasets (most accept latlng + range → "near a pin")
# --------------------------------------------------------------------------- #

async def _dataset(
    endpoint: str,
    fields: list[str],
    latlng: str | None,
    range: int | None,
    limit: int,
) -> list[dict[str, Any]]:
    params = {"latlng": latlng, "range": range, "limit": limit}
    data = await _get(endpoint, params)
    return _compact_features(data, fields, origin=_parse_latlng(latlng), limit=limit)


@mcp.tool()
async def air_quality(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Prague air-quality measuring stations (hourly index). `latlng`="lat,lon" + `range` (m) for 'near a pin'."""
    return await _dataset("/v2/airqualitystations/", ["name", "district", "measurement"], latlng, range, limit)


@mcp.tool()
async def parking_lots(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Prague parking locations (incl. P+R) with free-spot counts. `latlng`+`range` for nearby parking."""
    return await _dataset(
        "/v2/parking",
        ["name", "address_formatted", "category", "parking_type", "total_spot_number", "available_spots_number"],
        latlng,
        range,
        limit,
    )


@mcp.tool()
async def waste_stations(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Sorted-waste container stations. `latlng`+`range` for the nearest bins."""
    return await _dataset("/v2/sortedwastestations", ["name", "district", "station_number"], latlng, range, limit)


@mcp.tool()
async def bicycle_counters(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Prague bicycle-counter locations. `latlng`+`range` for nearby counters."""
    return await _dataset("/v2/bicyclecounters/", ["name", "route", "directions"], latlng, range, limit)


@mcp.tool()
async def shared_cars(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Car-sharing vehicles available in Prague (free-floating shared cars).

    Use to find a car-sharing car NEAR the user: pass `latlng`="lat,lon" (e.g. a Telegram
    location pin) + `range` (metres). Returns compact records sorted nearest-first with
    distance_m — provider/company, name/model, fuel, reservation link, plus coordinates.
    """
    # SharedCar.properties (Golemio output-gateway spec): name, res_url, company{name},
    # fuel{description}, availability{description} — the objects are flattened by _scalarize.
    return await _dataset(
        "/v2/sharedcars/",
        ["name", "company", "fuel", "availability", "res_url"],
        latlng,
        range,
        limit,
    )


@mcp.tool()
async def medical_institutions(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Hospitals/clinics/pharmacies in Prague. `latlng`+`range` for the nearest facilities."""
    return await _dataset("/v2/medicalinstitutions/", ["name", "address", "type", "telephone", "district"], latlng, range, limit)


@mcp.tool()
async def municipal_libraries(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Public municipal libraries. `latlng`+`range` for the nearest branch."""
    return await _dataset("/v2/municipallibraries/", ["name", "address", "telephone", "district"], latlng, range, limit)


@mcp.tool()
async def playgrounds(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Children's playgrounds. `latlng`+`range` for the nearest playground."""
    return await _dataset("/v2/playgrounds/", ["name", "address", "district", "url"], latlng, range, limit)


@mcp.tool()
async def gardens(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Public gardens/parks. `latlng`+`range` for the nearest green space."""
    return await _dataset("/v2/gardens/", ["name", "address", "district", "url"], latlng, range, limit)


@mcp.tool()
async def city_districts(latlng: str | None = None, range: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Prague city district boundaries/metadata. `latlng`+`range` to find the district at a point."""
    return await _dataset("/v2/citydistricts/", ["name", "slug", "id"], latlng, range, limit)


def main() -> None:
    # Accept an optional transport arg (`stdio`) for parity with the gramps launcher.
    transport = sys.argv[1] if len(sys.argv) > 1 else "stdio"
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
