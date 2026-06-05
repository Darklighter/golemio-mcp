"""Offline unit tests for golemio-mcp (no network / no API key needed)."""

import asyncio

from src.golemio_mcp import server


def test_haversine_known_distance():
    # Anděl ~ Můstek is roughly 1.6 km; allow a wide band.
    d = server._haversine_m(50.0712, 14.4036, 50.0833, 14.4213)
    assert 1000 < d < 2500


def test_parse_latlng():
    assert server._parse_latlng("50.0875,14.4213") == (50.0875, 14.4213)
    assert server._parse_latlng(None) is None
    assert server._parse_latlng("bad") is None


def test_compact_features_trims_and_sorts():
    data = {
        "features": [
            {"properties": {"stop_name": "Far", "extra": "drop"}, "geometry": {"type": "Point", "coordinates": [14.50, 50.10]}},
            {"properties": {"stop_name": "Near"}, "geometry": {"type": "Point", "coordinates": [14.42, 50.0875]}},
        ]
    }
    out = server._compact_features(data, ["stop_name"], origin=(50.0875, 14.4213), limit=10)
    assert [x["stop_name"] for x in out] == ["Near", "Far"]  # sorted nearest-first
    assert "extra" not in out[0]  # only requested fields kept
    assert "distance_m" in out[0] and "lat" in out[0] and "lon" in out[0]


def test_scalarize_flattens_nested_objects():
    assert server._scalarize({"name": "Anytime", "web": "x"}) == "Anytime"      # company
    assert server._scalarize({"id": 2, "description": "nafta"}) == "nafta"       # fuel/availability
    assert server._scalarize({"address_formatted": "Foo 1", "street_address": "Foo"}) == "Foo 1"  # address
    assert server._scalarize("plain") == "plain"
    assert server._scalarize(["a", "b"]) == ["a", "b"]  # arrays pass through


def test_compact_features_flattens_sharedcar_shape():
    data = {"features": [{
        "geometry": {"type": "Point", "coordinates": [14.4633, 50.07827]},
        "properties": {
            "name": "Peugeot 207",
            "res_url": "https://www.anytimecar.cz",
            "company": {"name": "Anytime", "web": "x"},
            "fuel": {"id": 2, "description": "nafta"},
            "availability": {"id": 2, "description": "dle domluvy"},
        },
    }]}
    out = server._compact_features(data, ["name", "company", "fuel", "availability", "res_url"], origin=(50.08, 14.46))
    assert out[0]["company"] == "Anytime"
    assert out[0]["fuel"] == "nafta"
    assert out[0]["availability"] == "dle domluvy"
    assert out[0]["name"] == "Peugeot 207"
    assert "distance_m" in out[0]


def test_fmt_departure_picks_predicted_time():
    dep = {
        "route": {"short_name": "22", "is_night": False},
        "trip": {"headsign": "Bílá Hora", "is_canceled": False},
        "departure_timestamp": {"predicted": "2026-06-05T18:03:00+02:00", "scheduled": "2026-06-05T18:01:00+02:00", "minutes": "3"},
        "delay": {"is_available": True, "minutes": 2},
        "stop": {"platform_code": "A"},
    }
    out = server._fmt_departure(dep)
    assert out["line"] == "22"
    assert out["headsign"] == "Bílá Hora"
    assert out["time"] == "18:03"
    assert out["minutes"] == "3"
    assert out["realtime"] is True


def test_norm_strips_diacritics():
    assert server._norm("Můstek") == server._norm("mustek") == "mustek"
    assert server._norm("Anděl") == "andel"


def test_gtfs_id_regex():
    assert server._GTFS_ID_RE.match("U400Z1P")
    assert server._GTFS_ID_RE.match("U7288Z1")
    assert not server._GTFS_ID_RE.match("Anděl")
    assert not server._GTFS_ID_RE.match("Můstek")


def test_group_compact_collects_gtfs_ids_and_distance():
    g = {
        "name": "Anděl", "fullName": "Anděl", "municipality": "Praha",
        "avgLat": 50.0712, "avgLon": 14.4036,
        "stops": [{"gtfsIds": ["U400Z1P"]}, {"gtfsIds": ["U400Z2P", "U400Z3P"]}],
    }
    out = server._group_compact(g, origin=(50.0833, 14.4213))
    assert out["gtfs_ids"] == ["U400Z1P", "U400Z2P", "U400Z3P"]
    assert out["stop_name"] == "Anděl"
    assert out["distance_m"] > 0


def test_resolve_gtfs_ids_direct_id_no_network():
    # A direct GTFS id short-circuits before any network call.
    ids, name = asyncio.run(server._resolve_gtfs_ids("U400Z1P", None, None))
    assert ids == ["U400Z1P"] and name == "U400Z1P"


def test_tools_registered():
    # Public FastMCP API: list_tools() returns Tool objects with .name.
    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"stop_search", "nearest_stops", "stop_departures", "air_quality", "shared_cars", "city_districts"} <= names
