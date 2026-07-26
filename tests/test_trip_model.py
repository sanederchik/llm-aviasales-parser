import datetime as dt

import pytest

from aviasales_search.trip_model import (
    GULF_AIRPORTS,
    ConfigError,
    Constraints,
    Direction,
    DirectionResult,
    FlightLeg,
    Itinerary,
    TimeOfDay,
    Ticket,
    parse_config,
)


def test_timeofday_contains_boundaries():
    assert TimeOfDay.MORNING.contains(dt.time(6, 0))
    assert TimeOfDay.MORNING.contains(dt.time(11, 59))
    assert not TimeOfDay.MORNING.contains(dt.time(12, 0))
    assert TimeOfDay.NIGHT.contains(dt.time(0, 0))
    assert TimeOfDay.NIGHT.contains(dt.time(5, 59))


# --------------------------- Constraints.merged_over ---------------------------

def test_constraints_merge_direction_overrides_global():
    base = Constraints(max_transfers=2, baggage_required=True)
    direction = Constraints(max_transfers=1)
    merged = direction.merged_over(base)
    assert merged.max_transfers == 1          # direction wins
    assert merged.baggage_required is True     # inherited from base


def test_constraints_merge_exclude_transfer_airports_new_field():
    base = Constraints(exclude_transfer_airports={"DXB", "DOH"})
    direction = Constraints()  # not set at direction level -> inherit from base
    merged = direction.merged_over(base)
    assert merged.exclude_transfer_airports == {"DXB", "DOH"}

    override = Constraints(exclude_transfer_airports={"IST"})
    merged2 = override.merged_over(base)
    assert merged2.exclude_transfer_airports == {"IST"}   # direction wins over base


# --------------------------- DirectionResult ---------------------------

def _ts(d: dt.datetime) -> int:
    """Treat naive test datetimes as UTC so timestamp diffs equal naive deltas
    exactly, independent of the machine's local timezone/DST."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="TK", flight_number="TK1"):
    return FlightLeg(
        origin, destination, dep, arr, carrier, flight_number,
        departure_ts=_ts(dep), arrival_ts=_ts(arr),
    )


def test_direction_result_transfers_duration_and_transfer_airports_direct():
    leg = _leg("MOW", "DPS", dt.datetime(2026, 9, 1, 10, 0), dt.datetime(2026, 9, 1, 22, 0))
    dr = DirectionResult(legs=[leg])
    assert dr.transfers == 0
    assert dr.duration_minutes == 12 * 60
    assert dr.transfer_airports == []
    assert dr.main_carrier == "TK"
    assert dr.depart == leg.departure
    assert dr.arrive == leg.arrival


def test_direction_result_transfers_duration_and_transfer_airports_with_stops():
    leg1 = _leg("MOW", "IST", dt.datetime(2026, 8, 25, 10, 0), dt.datetime(2026, 8, 25, 13, 5), carrier="TK")
    leg2 = _leg("IST", "DOH", dt.datetime(2026, 8, 25, 15, 5), dt.datetime(2026, 8, 25, 19, 0), carrier="QR")
    leg3 = _leg("DOH", "DPS", dt.datetime(2026, 8, 25, 21, 0), dt.datetime(2026, 8, 26, 5, 5), carrier="QR")
    dr = DirectionResult(legs=[leg1, leg2, leg3])
    assert dr.transfers == 2
    assert dr.duration_minutes == int((leg3.arrival - leg1.departure).total_seconds() // 60)
    assert dr.transfer_airports == ["IST", "DOH"]  # destinations of all but last leg
    assert dr.main_carrier == "TK"
    assert dr.depart == leg1.departure
    assert dr.arrive == leg3.arrival


# --------------------------- Ticket.route ---------------------------

def test_ticket_route_single_direction():
    leg = _leg("MOW", "DPS", dt.datetime(2026, 9, 1, 10, 0), dt.datetime(2026, 9, 1, 22, 0))
    t = Ticket(price_rub=100000, directions=[DirectionResult(legs=[leg])],
               has_baggage=True, deep_link="https://x")
    assert t.route == "MOW→DPS"


def test_ticket_route_round_trip_joins_directions():
    out_leg = _leg("MOW", "DPS", dt.datetime(2026, 9, 1, 10, 0), dt.datetime(2026, 9, 1, 22, 0))
    back_leg = _leg("DPS", "MOW", dt.datetime(2026, 12, 1, 10, 0), dt.datetime(2026, 12, 1, 22, 0))
    t = Ticket(
        price_rub=160423,
        directions=[DirectionResult(legs=[out_leg]), DirectionResult(legs=[back_leg])],
        has_baggage=True,
        deep_link="https://x",
    )
    assert t.route == "MOW→DPS ⇄ DPS→MOW"


# --------------------------- parse_config ---------------------------

def _minimal_direction(from_="MOW", to="DPS", earliest="2026-09-01", latest="2026-09-30"):
    return {"from": from_, "to": to, "date_window": {"earliest": earliest, "latest": latest}}


def test_parse_config_minimal_ok_from_to_and_windows():
    cfg = parse_config({"directions": [_minimal_direction()]})
    assert cfg.currency == "rub"
    assert cfg.market_code == "ru"
    assert cfg.trip_class == "Y"
    assert cfg.passengers.adults == 1
    assert len(cfg.directions) == 1
    d = cfg.directions[0]
    assert isinstance(d, Direction)
    assert d.origin == "MOW"
    assert d.destination == "DPS"
    assert d.date_window.earliest == dt.date(2026, 9, 1)
    assert d.date_window.latest == dt.date(2026, 9, 30)


def test_parse_config_full_schema_from_brief():
    cfg = parse_config({
        "currency": "rub", "market_code": "ru", "trip_class": "Y",
        "passengers": {"adults": 2, "children": 0, "infants": 0},
        "directions": [
            {"from": "MOW", "to": "DPS",
             "date_window": {"earliest": "2026-09-01", "latest": "2026-09-30"},
             "constraints": {"max_transfers": 1, "baggage_required": True,
                              "max_duration_minutes": 1440, "exclude_gulf_transfers": True}},
            {"from": "DPS", "to": "MOW",
             "date_window": {"earliest": "2026-12-01", "latest": "2026-12-31"}},
        ],
        "global_constraints": {"max_transfers": 1, "baggage_required": True,
                                "max_duration_minutes": 1440, "exclude_gulf_transfers": True},
        "search_budget": {"max_requests": 40, "date_samples_per_direction": 4},
        "cache": {"ttl_hours": 24},
    })
    assert isinstance(cfg, Itinerary)
    assert cfg.passengers.adults == 2
    assert len(cfg.directions) == 2
    assert cfg.directions[1].origin == "DPS"
    assert cfg.directions[1].destination == "MOW"
    assert cfg.search_budget.max_requests == 40
    assert cfg.search_budget.date_samples_per_direction == 4
    assert cfg.cache.ttl_minutes == 24 * 60


def test_parse_config_exclude_gulf_transfers_maps_to_gulf_airports_set():
    cfg = parse_config({
        "directions": [{**_minimal_direction(),
                        "constraints": {"exclude_gulf_transfers": True}}],
    })
    eff = cfg.effective_constraints(0)
    assert eff.exclude_transfer_airports == set(GULF_AIRPORTS)


def test_parse_config_explicit_exclude_transfer_airports_list():
    cfg = parse_config({
        "directions": [{**_minimal_direction(),
                        "constraints": {"exclude_transfer_airports": ["IST", "AYT"]}}],
    })
    eff = cfg.effective_constraints(0)
    assert eff.exclude_transfer_airports == {"IST", "AYT"}


def test_parse_config_default_cache_ttl_5_minutes():
    cfg = parse_config({"directions": [_minimal_direction()]})
    assert cfg.cache.ttl_minutes == 5


def test_parse_config_cache_ttl_minutes_parsed():
    cfg = parse_config({
        "directions": [_minimal_direction()], "cache": {"ttl_minutes": 30},
    })
    assert cfg.cache.ttl_minutes == 30


def test_parse_config_cache_legacy_ttl_hours_converted_to_minutes():
    cfg = parse_config({
        "directions": [_minimal_direction()], "cache": {"ttl_hours": 2},
    })
    assert cfg.cache.ttl_minutes == 120


def test_parse_config_default_search_budget_100_requests_10_samples():
    cfg = parse_config({"directions": [_minimal_direction()]})
    assert cfg.search_budget.max_requests == 100
    assert cfg.search_budget.date_samples_per_direction == 10


def test_parse_config_max_trip_days_parsed():
    cfg = parse_config({
        "directions": [
            _minimal_direction(),
            _minimal_direction(from_="DPS", to="MOW",
                               earliest="2026-11-01", latest="2026-11-30"),
        ],
        "max_trip_days": 55,
    })
    assert cfg.max_trip_days == 55


def test_parse_config_max_trip_days_default_none():
    cfg = parse_config({"directions": [_minimal_direction()]})
    assert cfg.max_trip_days is None


def test_parse_config_rejects_non_positive_max_trip_days():
    with pytest.raises(ConfigError):
        parse_config({"directions": [_minimal_direction()], "max_trip_days": 0})


def test_parse_config_rejects_direction_without_date_window():
    with pytest.raises(ConfigError):
        parse_config({"directions": [{"from": "MOW", "to": "DPS"}]})


def test_parse_config_rejects_second_direction_without_date_window():
    with pytest.raises(ConfigError):
        parse_config({
            "directions": [
                _minimal_direction(),
                {"from": "DPS", "to": "MOW"},
            ]
        })


def test_parse_config_rejects_empty_directions_list():
    with pytest.raises(ConfigError):
        parse_config({"directions": []})


def test_parse_config_rejects_missing_directions_key():
    with pytest.raises(ConfigError):
        parse_config({})


def test_parse_config_rejects_bad_time_of_day():
    with pytest.raises(ConfigError):
        parse_config({
            "directions": [{**_minimal_direction(),
                            "constraints": {"depart_time_of_day": ["lunchtime"]}}]
        })


def test_parse_config_rejects_latest_before_earliest():
    with pytest.raises(ConfigError):
        parse_config({
            "directions": [{"from": "MOW", "to": "DPS",
                             "date_window": {"earliest": "2026-09-30", "latest": "2026-09-01"}}]
        })


def test_parse_config_date_window_allows_earliest_equals_latest():
    cfg = parse_config({
        "directions": [_minimal_direction(earliest="2026-09-15", latest="2026-09-15")],
    })
    assert cfg.directions[0].date_window.earliest == cfg.directions[0].date_window.latest


# --------------------------- effective_constraints ---------------------------

def test_effective_constraints_merges_per_direction_over_global():
    cfg = parse_config({
        "global_constraints": {"baggage_required": True, "max_transfers": 2},
        "directions": [
            {**_minimal_direction(), "constraints": {"max_transfers": 0}},
        ],
    })
    eff = cfg.effective_constraints(0)
    assert eff.max_transfers == 0          # direction override wins
    assert eff.baggage_required is True    # inherited from global


def test_effective_constraints_indexes_per_direction_independently():
    cfg = parse_config({
        "global_constraints": {"max_transfers": 2},
        "directions": [
            {**_minimal_direction(), "constraints": {"max_transfers": 0}},
            {**_minimal_direction(from_="DPS", to="MOW",
                                  earliest="2026-12-01", latest="2026-12-31")},
        ],
    })
    assert cfg.effective_constraints(0).max_transfers == 0
    assert cfg.effective_constraints(1).max_transfers == 2  # falls back to global
