from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


def probe_key(dated_directions: list[tuple], adults: int, children: int, infants: int,
              trip_class: str, baggage_required: bool = False) -> str:
    """
    Generate a stable SHA1 cache key for a search probe.

    Args:
        dated_directions: List of (origin, destination, date_iso) tuples/lists.
                         Each leg represents a direction in the trip.
        adults: Number of adult passengers.
        children: Number of child passengers.
        infants: Number of infant passengers.
        trip_class: Cabin class (e.g., 'Y' for economy, 'J' for business).
        baggage_required: Whether the search was run requiring baggage. This
                         changes what SearchClient.search/results_parser return
                         (no-baggage fares are dropped, prices differ), so it
                         must be part of the key to avoid a baggage-free probe
                         being reused for a baggage-required search or vice versa.

    Returns:
        Hexadecimal SHA1 hash string.

    Stability guarantees:
    - Same inputs (even if lists instead of tuples) produce the same key.
    - Order of directions matters (e.g., MOW->LED->IST != LED->IST->MOW).
    - Different passengers, class, or baggage_required produce different keys.
    - Omitting baggage_required is equivalent to passing False: same-version calls
      with/without the kwarg agree. Entries written before this parameter existed
      are invalidated once when the code is upgraded (harmless - just one extra
      network probe per stale key, not a correctness issue).
    """
    # Normalize: convert all direction entries to tuples for consistent representation
    normalized_directions = [tuple(d) for d in dated_directions]

    # Create a canonical dictionary for hashing
    canonical = {
        "directions": normalized_directions,
        "adults": adults,
        "children": children,
        "infants": infants,
        "trip_class": trip_class,
        "baggage_required": baggage_required,
    }

    # JSON serialize with sorted keys for stable output
    canonical_json = json.dumps(canonical, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(canonical_json.encode()).hexdigest()


def probe_cache_key(dated_directions, passengers, trip_class: str,
                    baggage_required: bool, min_baggage_weight_kg,
                    filters_state: dict) -> str:
    """Полный ключ пробы, общий для Фазы 1 (LegSweeper) и Фазы 2 (ComboVerifier):
    SHA1-база (`probe_key`) плюс суффикс `:{min_weight}:{filters_state}`. Обе
    фазы ОБЯЗАНЫ строить ключ одинаково — иначе пробы либо молча не
    переиспользуются, либо (при выпадении суффикса) переиспользуются пробы,
    снятые под другими фильтрами/весом багажа. min_baggage_weight_kg выведен из
    per-direction constraints и НЕ входит в filters_state (тот отражает только
    global_constraints), поэтому включается в ключ отдельно."""
    base = probe_key(
        dated_directions, passengers.adults, passengers.children, passengers.infants,
        trip_class, baggage_required=baggage_required,
    )
    return f"{base}:{min_baggage_weight_kg}:{json.dumps(filters_state, sort_keys=True)}"


@dataclass
class ProbeCache:
    path: Path

    def put(self, key: str, tickets: list[dict], now: dt.datetime) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"key": key, "fetched_at": now.isoformat(), "tickets": tickets}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get(self, key: str, ttl_minutes: int, now: dt.datetime) -> list[dict] | None:
        if not self.path.exists():
            return None
        freshest_at: dt.datetime | None = None
        freshest_tickets: list[dict] | None = None
        with self.path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec["key"] != key:
                    continue
                fetched = dt.datetime.fromisoformat(rec["fetched_at"])
                if freshest_at is None or fetched > freshest_at:
                    freshest_at = fetched
                    freshest_tickets = rec["tickets"]
        if freshest_at is None:
            return None
        if now - freshest_at > dt.timedelta(minutes=ttl_minutes):
            return None
        return freshest_tickets


def _run_dirname(run_ts: dt.datetime) -> str:
    return run_ts.strftime("%Y-%m-%dT%H-%M-%S")


def write_snapshot(cache_dir: Path, run_ts: dt.datetime, trip: dict,
                   offers: list[dict], report_md: str) -> Path:
    run_dir = cache_dir / "runs" / _run_dirname(run_ts)
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "trip.json").write_text(
        json.dumps(trip, ensure_ascii=False, indent=2), encoding="utf-8")
    with (run_dir / "offers.jsonl").open("w", encoding="utf-8") as f:
        for offer in offers:
            f.write(json.dumps(offer, ensure_ascii=False) + "\n")
    (run_dir / "report.md").write_text(report_md, encoding="utf-8")
    return run_dir


_TRIP_IDENTITY_KEYS = ("currency", "passengers", "trip_class", "directions", "global_constraints")


def _trip_identity(trip: dict) -> str:
    """Каноническое представление маршрутообразующих полей конфига (без
    search_budget/cache), по которому сравниваются поездки между запусками."""
    subset = {key: trip.get(key) for key in _TRIP_IDENTITY_KEYS}
    return json.dumps(subset, sort_keys=True, ensure_ascii=False)


def latest_previous_offers(cache_dir: Path, before_ts: dt.datetime,
                           trip: dict | None = None) -> list[dict] | None:
    runs_dir = cache_dir / "runs"
    if not runs_dir.exists():
        return None
    before_name = _run_dirname(before_ts)
    identity = _trip_identity(trip) if trip is not None else None
    candidates = []
    for d in runs_dir.iterdir():
        if not d.is_dir() or d.name >= before_name or not (d / "offers.jsonl").exists():
            continue
        if identity is not None:
            trip_path = d / "trip.json"
            if not trip_path.exists():
                continue
            candidate_trip = json.loads(trip_path.read_text(encoding="utf-8"))
            if _trip_identity(candidate_trip) != identity:
                continue
        candidates.append(d)
    candidates.sort(key=lambda d: d.name)
    if not candidates:
        return None
    offers = []
    with (candidates[-1] / "offers.jsonl").open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                offers.append(json.loads(line))
    return offers
