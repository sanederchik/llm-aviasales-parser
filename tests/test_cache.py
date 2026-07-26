import datetime as dt
import json

from aviasales_search.cache import (
    ProbeCache,
    latest_previous_offers,
    probe_key,
    write_snapshot,
)


def test_probe_key_stable_with_tuples():
    """Same dated_directions (as tuples) should produce same key."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    assert k1 == k2


def test_probe_key_stable_with_lists():
    """Same dated_directions (as lists normalized to tuples) should produce same key."""
    # Input as lists should be normalized to same key
    k1 = probe_key([["MOW", "LED", "2026-08-01"]], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    assert k1 == k2


def test_probe_key_different_directions():
    """Different directions should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "IST", "2026-08-01")], 1, 0, 0, "Y")
    assert k1 != k2


def test_probe_key_different_dates():
    """Different dates should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-02")], 1, 0, 0, "Y")
    assert k1 != k2


def test_probe_key_different_adults():
    """Different adult counts should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 2, 0, 0, "Y")
    assert k1 != k2


def test_probe_key_different_children():
    """Different children counts should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 1, 0, "Y")
    assert k1 != k2


def test_probe_key_different_infants():
    """Different infant counts should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 1, "Y")
    assert k1 != k2


def test_probe_key_different_trip_class():
    """Different trip classes should produce different keys."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "J")
    assert k1 != k2


def test_probe_key_different_baggage_required():
    """Different baggage_required should produce different keys (baggage_required
    changes what SearchClient.search/results_parser returns: no-baggage fares are
    dropped and prices differ, so it must be part of the cache key)."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y", baggage_required=False)
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y", baggage_required=True)
    assert k1 != k2


def test_probe_key_baggage_required_defaults_to_false_preserves_prior_behavior():
    """Omitting baggage_required must produce the same key as explicit False, so
    existing cache entries written before this parameter existed remain valid."""
    k1 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y", baggage_required=False)
    assert k1 == k2


def test_probe_key_multi_leg_trip():
    """Multi-leg trip (multi-city) should produce consistent key."""
    # Same multi-leg directions in same order
    k1 = probe_key([("MOW", "LED", "2026-08-01"), ("LED", "IST", "2026-08-05")], 1, 0, 0, "Y")
    k2 = probe_key([("MOW", "LED", "2026-08-01"), ("LED", "IST", "2026-08-05")], 1, 0, 0, "Y")
    assert k1 == k2
    # Different order should produce different key
    k3 = probe_key([("LED", "IST", "2026-08-05"), ("MOW", "LED", "2026-08-01")], 1, 0, 0, "Y")
    assert k1 != k3


def test_probe_cache_hit_within_ttl(tmp_path):
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0, 0)
    cache.put("k", [{"price_rub": 100}], now)
    got = cache.get("k", ttl_minutes=24 * 60, now=now + dt.timedelta(hours=1))
    assert got == [{"price_rub": 100}]


def test_probe_cache_miss_when_stale(tmp_path):
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0, 0)
    cache.put("k", [{"price_rub": 100}], now)
    got = cache.get("k", ttl_minutes=24 * 60, now=now + dt.timedelta(hours=25))
    assert got is None


def test_probe_cache_returns_freshest(tmp_path):
    cache = ProbeCache(tmp_path / "probes.jsonl")
    base = dt.datetime(2026, 7, 26, 12, 0, 0)
    cache.put("k", [{"price_rub": 100}], base)
    cache.put("k", [{"price_rub": 90}], base + dt.timedelta(hours=2))
    got = cache.get("k", ttl_minutes=24 * 60, now=base + dt.timedelta(hours=3))
    assert got == [{"price_rub": 90}]


def test_write_snapshot_and_read_previous(tmp_path):
    ts1 = dt.datetime(2026, 7, 26, 10, 0, 0)
    p = write_snapshot(tmp_path, ts1, {"legs": []},
                       [{"total_rub": 200}, {"total_rub": 100}], "# report")
    assert (p / "trip.json").exists()
    assert (p / "report.md").read_text() == "# report"
    lines = (p / "offers.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["total_rub"] == 200  # порядок сохранён как передан

    ts2 = dt.datetime(2026, 7, 26, 14, 0, 0)
    prev = latest_previous_offers(tmp_path, before_ts=ts2)
    assert prev[0]["total_rub"] == 200


def test_latest_previous_none_when_no_earlier(tmp_path):
    ts1 = dt.datetime(2026, 7, 26, 10, 0, 0)
    write_snapshot(tmp_path, ts1, {"legs": []}, [{"total_rub": 1}], "r")
    assert latest_previous_offers(tmp_path, before_ts=ts1) is None


def test_latest_previous_offers_filters_by_directions_key(tmp_path):
    """v3.2 configs key their route as 'directions' (not the old 'legs'); the
    identity check must use the field that actually exists in current configs,
    otherwise two runs with different routes would be treated as the same trip
    (bogus price deltas)."""
    trip_a = {"directions": [{"from": "MOW", "to": "IST"}]}
    trip_b = {"directions": [{"from": "MOW", "to": "LED"}]}
    ts1 = dt.datetime(2026, 7, 26, 8, 0, 0)
    ts2 = dt.datetime(2026, 7, 26, 10, 0, 0)
    write_snapshot(tmp_path, ts1, trip_a, [{"total_rub": 111}], "r")  # A
    write_snapshot(tmp_path, ts2, trip_b, [{"total_rub": 222}], "r")  # B, different route

    after = dt.datetime(2026, 7, 26, 12, 0, 0)

    # Same directions (A) IS offered as previous.
    prev_a = latest_previous_offers(tmp_path, before_ts=after, trip=trip_a)
    assert prev_a[0]["total_rub"] == 111

    # Different directions (B's run) must NOT be offered as A's previous, even
    # though it's the more recent run overall.
    assert prev_a != [{"total_rub": 222}]


def test_latest_previous_offers_filters_by_same_trip(tmp_path):
    trip_a = {"directions": [{"from": "MOW", "to": "IST"}]}
    trip_b = {"directions": [{"from": "MOW", "to": "LED"}]}
    ts1 = dt.datetime(2026, 7, 26, 8, 0, 0)
    ts2 = dt.datetime(2026, 7, 26, 10, 0, 0)
    ts3 = dt.datetime(2026, 7, 26, 12, 0, 0)
    write_snapshot(tmp_path, ts1, trip_a, [{"total_rub": 111}], "r")   # A, older
    write_snapshot(tmp_path, ts2, trip_b, [{"total_rub": 222}], "r")  # B, newer, unrelated trip
    write_snapshot(tmp_path, ts3, trip_a, [{"total_rub": 333}], "r")  # A, newest

    after_ts3 = dt.datetime(2026, 7, 26, 14, 0, 0)

    # Filtered by trip=A: must skip the newer-but-different trip B (ts2) run,
    # and skip A's own ts3 run (not strictly before before_ts=ts3), landing on A's ts1.
    prev_same_trip = latest_previous_offers(tmp_path, before_ts=ts3, trip=trip_a)
    assert prev_same_trip[0]["total_rub"] == 111

    # Without a trip filter, the newest prior run (any trip) is B's ts2.
    prev_any_trip = latest_previous_offers(tmp_path, before_ts=ts3)
    assert prev_any_trip[0]["total_rub"] == 222

    # Filtered by trip=A relative to after_ts3: newest A run is ts3.
    prev_same_trip_latest = latest_previous_offers(tmp_path, before_ts=after_ts3, trip=trip_a)
    assert prev_same_trip_latest[0]["total_rub"] == 333
