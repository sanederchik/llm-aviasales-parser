import datetime as dt
import logging

from aviasales_search.cache import ProbeCache
from aviasales_search.planner import (
    search_page_url,
    Itin,
    Planner,
    _ticket_from_dict,
    _ticket_to_dict,
    date_combinations,
    sample_dates,
)
from aviasales_search.trip_model import (
    CacheCfg,
    Constraints,
    DateWindow,
    Direction,
    DirectionResult,
    FlightLeg,
    Itinerary,
    Passengers,
    SearchBudget,
    Ticket,
)

# --------------------------- builders ---------------------------


def _ts(d: dt.datetime) -> int:
    """Treat naive test datetimes as UTC so timestamp diffs equal naive deltas
    exactly, independent of the machine's local timezone/DST."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="TK", flight="TK1"):
    return FlightLeg(
        origin=origin, destination=destination, departure=dep, arrival=arr,
        carrier=carrier, flight_number=flight,
        departure_ts=_ts(dep), arrival_ts=_ts(arr),
    )


def _direct_direction(origin, destination, date_iso, dep_hour=10, duration_hours=3):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr = dep + dt.timedelta(hours=duration_hours)
    return DirectionResult(legs=[_leg(origin, destination, dep, arr)])


def _transfer_direction(origin, hub, destination, date_iso, gap_minutes=120,
                        leg_hours=3, dep_hour=10):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr1 = dep + dt.timedelta(hours=leg_hours)
    dep2 = arr1 + dt.timedelta(minutes=gap_minutes)
    arr2 = dep2 + dt.timedelta(hours=leg_hours)
    return DirectionResult(legs=[
        _leg(origin, hub, dep, arr1),
        _leg(hub, destination, dep2, arr2),
    ])


def _ticket(directions, price=1000, has_baggage=True):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage, deep_link="x")


def _direction_cfg(origin, destination, earliest, latest):
    return Direction(
        origin=origin, destination=destination,
        date_window=DateWindow(
            earliest=dt.date.fromisoformat(earliest), latest=dt.date.fromisoformat(latest),
        ),
    )


def _itinerary(directions, global_c=None, per_direction_cs=None, max_requests=40,
              date_samples_per_direction=4, max_trip_days=None):
    n = len(directions)
    return Itinerary(
        currency="rub",
        market_code="ru",
        trip_class="Y",
        passengers=Passengers(adults=2),
        directions=directions,
        global_constraints=global_c if global_c is not None else Constraints(),
        per_direction_constraints=(
            per_direction_cs if per_direction_cs is not None else [Constraints() for _ in range(n)]
        ),
        search_budget=SearchBudget(
            max_requests=max_requests, date_samples_per_direction=date_samples_per_direction,
        ),
        cache=CacheCfg(),
        max_trip_days=max_trip_days,
    )


class FakeClient:
    """Fake `SearchClient`: returns tickets keyed by the tuple of ISO date strings
    used in the probe (one date per direction, in order)."""

    def __init__(self, tickets_by_dates: dict, default=None):
        self.tickets_by_dates = tickets_by_dates
        self.default = default if default is not None else []
        self.calls: list[list] = []
        self.baggage_flags: list[bool] = []

    def search(self, dated_directions, passengers, trip_class, market_code, currency_code,
              baggage_required=False):
        self.calls.append(list(dated_directions))
        self.baggage_flags.append(baggage_required)
        dates = tuple(d[2] for d in dated_directions)
        return self.tickets_by_dates.get(dates, self.default)


# --------------------------- sample_dates ---------------------------


def test_sample_dates_includes_ends():
    days = [dt.date(2026, 8, d) for d in range(1, 11)]
    picked = sample_dates(days, 3)
    assert picked[0] == days[0]
    assert picked[-1] == days[-1]
    assert len(picked) == 3


# --------------------------- date_combinations ---------------------------


def test_date_combinations_respects_samples_per_direction():
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-29")]  # 10 days
    itinerary = _itinerary(directions)
    combos = date_combinations(itinerary, samples=3)
    assert len(combos) == 3


def test_date_combinations_enforces_monotonicity_across_directions():
    directions = [
        _direction_cfg("MOW", "IST", "2026-08-20", "2026-08-25"),
        _direction_cfg("IST", "MOW", "2026-08-18", "2026-08-30"),
    ]
    itinerary = _itinerary(directions)
    combos = date_combinations(itinerary, samples=4)
    assert combos
    for combo in combos:
        assert combo[0] <= combo[1]
    # some (date0, date1) pairs from the raw cartesian product violate monotonicity
    # (e.g. date0=2026-08-25, date1=2026-08-18) and must be excluded
    assert (dt.date(2026, 8, 25), dt.date(2026, 8, 18)) not in combos


def test_date_combinations_enforces_max_trip_days():
    directions = [
        _direction_cfg("MOW", "DPS", "2026-09-07", "2026-09-30"),
        _direction_cfg("DPS", "MOW", "2026-11-01", "2026-11-30"),
    ]
    itinerary = _itinerary(directions, max_trip_days=55)
    combos = date_combinations(itinerary, samples=4)
    assert combos
    for combo in combos:
        assert (combo[-1] - combo[0]).days <= 55
    # раскладка без лимита содержит, например, 2026-09-07 → 2026-11-30 (84 дня)
    assert (dt.date(2026, 9, 7), dt.date(2026, 11, 30)) not in combos


def test_date_combinations_no_max_trip_days_keeps_all_monotonic_combos():
    directions = [
        _direction_cfg("MOW", "DPS", "2026-09-07", "2026-09-30"),
        _direction_cfg("DPS", "MOW", "2026-11-01", "2026-11-30"),
    ]
    itinerary = _itinerary(directions)
    combos = date_combinations(itinerary, samples=4)
    assert (dt.date(2026, 9, 7), dt.date(2026, 11, 30)) in combos


def test_date_combinations_single_direction_is_plain_sample():
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-29")]
    itinerary = _itinerary(directions)
    combos = date_combinations(itinerary, samples=4)
    assert len(combos) == 4
    assert all(len(c) == 1 for c in combos)


# --------------------------- Itin ---------------------------


def test_itin_best_ticket_min_price():
    itin = Itin(
        dated_dirs=(dt.date(2026, 8, 25),),
        tickets=[
            _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=300),
            _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150),
        ],
    )
    assert itin.best_ticket.price_rub == 150


def test_itin_best_ticket_none_when_no_tickets():
    itin = Itin(dated_dirs=(dt.date(2026, 8, 25),), tickets=[])
    assert itin.best_ticket is None


# --------------------------- _ticket_to_dict / _ticket_from_dict roundtrip ---------------------------


def test_ticket_dict_roundtrip_preserves_ts_and_carrier_name():
    """Regression: departure_ts/arrival_ts (Fix 1, timezone-correct durations)
    and carrier_name (Fix 2, airline display name) must survive a cache
    put/get roundtrip, otherwise duration_minutes and the report's airline
    name would silently revert to defaults (0 / bare IATA code) for any
    ticket served from cache instead of freshly parsed."""
    leg = FlightLeg(
        origin="SVO", destination="CAN",
        departure=dt.datetime(2026, 9, 15, 21, 15),
        arrival=dt.datetime(2026, 9, 16, 2, 30),
        carrier="CZ", flight_number="625",
        departure_ts=1789499700, arrival_ts=1789529400,
        carrier_name="China Southern Airlines",
    )
    ticket = _ticket([DirectionResult(legs=[leg])], price=160423)

    restored = _ticket_from_dict(_ticket_to_dict(ticket))

    restored_leg = restored.directions[0].legs[0]
    assert restored_leg.departure_ts == 1789499700
    assert restored_leg.arrival_ts == 1789529400
    assert restored_leg.carrier_name == "China Southern Airlines"
    assert restored.directions[0].duration_minutes == ticket.directions[0].duration_minutes
    assert restored.directions[0].main_carrier_name == "China Southern Airlines"


# --------------------------- Planner ---------------------------


def test_planner_ranks_tickets_by_price_ascending(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    tickets_by_dates = {
        ("2026-08-25",): [
            _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=300),
            _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150),
        ],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    planner = Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0))
    tickets = planner.plan()
    assert [t.price_rub for t in tickets] == [150, 300]


def test_planner_uses_cache_second_run_zero_network_calls(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    tickets_by_dates = {
        ("2026-08-25",): [_ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)
    Planner(itinerary, client, cache, now=now).plan()
    assert len(client.calls) == 1

    # второй прогон внутри дефолтного TTL (5 минут) — целиком из кэша
    tickets2 = Planner(itinerary, client, cache, now=now + dt.timedelta(minutes=2)).plan()
    assert len(client.calls) == 1  # second run served entirely from cache
    assert tickets2[0].price_rub == 150
    assert tickets2[0].route == "MOW→IST"  # cache roundtrip preserves ticket data


def test_planner_refresh_forces_network(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    tickets_by_dates = {
        ("2026-08-25",): [_ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)
    Planner(itinerary, client, cache, now=now).plan()
    Planner(itinerary, client, cache, now=now, refresh=True).plan()
    assert len(client.calls) == 2


def test_planner_budget_caps_network_calls_but_cache_hits_are_free(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-23")]  # 4 days
    itinerary = _itinerary(directions, max_requests=1, date_samples_per_direction=4)
    tickets_by_dates = {
        ("2026-08-20",): [_ticket([_direct_direction("MOW", "IST", "2026-08-20")], price=200)],
        ("2026-08-21",): [_ticket([_direct_direction("MOW", "IST", "2026-08-21")], price=150)],
        ("2026-08-22",): [_ticket([_direct_direction("MOW", "IST", "2026-08-22")], price=300)],
        ("2026-08-23",): [_ticket([_direct_direction("MOW", "IST", "2026-08-23")], price=400)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)

    tickets1 = Planner(itinerary, client, cache, now=now).plan()
    assert len(client.calls) == 1
    assert [t.price_rub for t in tickets1] == [200]  # only the 1st combo got network budget

    # 2nd run: the 08-20 date is now a free cache hit; budget=1 still lets exactly one
    # more NEW network call happen (08-21), proving cache hits don't consume the budget.
    tickets2 = Planner(itinerary, client, cache, now=now).plan()
    assert len(client.calls) == 2
    assert [t.price_rub for t in tickets2] == [150, 200]


def test_planner_logs_when_combinations_exceed_budget(tmp_path, caplog):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-23")]  # 4 days
    itinerary = _itinerary(directions, max_requests=1, date_samples_per_direction=4)
    client = FakeClient({})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)
    with caplog.at_level(logging.INFO, logger="aviasales_search.planner"):
        Planner(itinerary, client, cache, now=now).plan()
    assert any("budget" in rec.message.lower() for rec in caplog.records)


def test_planner_applies_passes_itinerary_to_exclude_violating_tickets(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    itinerary = _itinerary(
        directions, per_direction_cs=[Constraints(max_transfers=0)], date_samples_per_direction=1,
    )
    tickets_by_dates = {
        ("2026-08-25",): [
            _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150),
            _ticket([_transfer_direction("MOW", "AUH", "IST", "2026-08-25")], price=100),
        ],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    planner = Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0))
    tickets = planner.plan()
    assert len(tickets) == 1
    assert tickets[0].price_rub == 150


def test_planner_derives_baggage_required_from_any_direction(tmp_path):
    directions = [
        _direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25"),
        _direction_cfg("IST", "MOW", "2026-08-26", "2026-08-26"),
    ]
    itinerary = _itinerary(
        directions,
        per_direction_cs=[Constraints(), Constraints(baggage_required=True)],
        date_samples_per_direction=1,
    )
    tickets_by_dates = {
        ("2026-08-25", "2026-08-26"): [
            _ticket([
                _direct_direction("MOW", "IST", "2026-08-25"),
                _direct_direction("IST", "MOW", "2026-08-26"),
            ], price=150, has_baggage=True),
        ],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    planner = Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0))
    planner.plan()
    assert client.baggage_flags == [True]


def test_planner_baggage_not_required_when_no_direction_requires_it(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    tickets_by_dates = {
        ("2026-08-25",): [_ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    planner = Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0))
    planner.plan()
    assert client.baggage_flags == [False]


def test_planner_baggage_required_change_does_not_reuse_stale_cache_entry(tmp_path):
    """Regression: baggage_required changes what SearchClient.search returns
    (results_parser drops no-baggage fares / prices differ), so a cache entry
    written by a baggage-free run must NOT be reused by a baggage-required run
    for the same route/dates/pax/class -> the 2nd run must hit the network again."""
    directions = [_direction_cfg("MOW", "IST", "2026-08-25", "2026-08-25")]
    tickets_by_dates = {
        ("2026-08-25",): [_ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)],
    }
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)

    no_baggage_itin = _itinerary(directions, date_samples_per_direction=1)
    client = FakeClient(tickets_by_dates)
    Planner(no_baggage_itin, client, cache, now=now).plan()
    assert len(client.calls) == 1
    assert client.baggage_flags == [False]

    baggage_itin = _itinerary(
        directions,
        per_direction_cs=[Constraints(baggage_required=True)],
        date_samples_per_direction=1,
    )
    Planner(baggage_itin, client, cache, now=now).plan()
    assert len(client.calls) == 2  # must NOT reuse the baggage-free cache entry
    assert client.baggage_flags == [False, True]


# --------------------------- deep links ---------------------------


def test_search_page_url_round_trip_matches_doc_example():
    url = search_page_url(
        [("MOW", "DPS", "2026-09-15"), ("DPS", "MOW", "2026-12-15")],
        Passengers(adults=2),
    )
    assert url == "https://www.aviasales.ru/search/MOW1509DPS15122"


def test_search_page_url_one_way_single_adult():
    url = search_page_url([("MOW", "IST", "2026-10-13")], Passengers(adults=1))
    assert url == "https://www.aviasales.ru/search/MOW1310IST1"


def test_search_page_url_passenger_digits_keep_inner_zero():
    assert search_page_url(
        [("MOW", "IST", "2026-10-13")], Passengers(adults=1, children=2),
    ).endswith("IST12")
    assert search_page_url(
        [("MOW", "IST", "2026-10-13")], Passengers(adults=1, infants=2),
    ).endswith("IST102")


def test_search_page_url_unsupported_shapes_return_empty():
    three = [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08"),
             ("TAS", "MOW", "2026-10-20")]
    assert search_page_url(three, Passengers(adults=1)) == ""
    open_jaw = [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08")]
    assert search_page_url(open_jaw, Passengers(adults=1)) == ""


def test_plan_populates_deep_link_including_cache_hits(tmp_path):
    directions = [
        _direction_cfg("MOW", "IST", "2026-10-13", "2026-10-13"),
        _direction_cfg("IST", "MOW", "2026-10-20", "2026-10-20"),
    ]
    ticket = _ticket(
        [_direct_direction("SVO", "IST", "2026-10-13"),
         _direct_direction("IST", "SVO", "2026-10-20")],
        price=30000,
    )
    client = FakeClient({("2026-10-13", "2026-10-20"): [ticket]})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    expected = "https://www.aviasales.ru/search/MOW1310IST20102"

    got = Planner(config=_itinerary(directions), client=client, cache=cache,
                  now=dt.datetime(2026, 7, 26, 12, 0)).plan()
    assert [t.deep_link for t in got] == [expected]

    # повторный прогон внутри дефолтного TTL (5 минут) — кэш-хит
    cached = Planner(config=_itinerary(directions), client=FakeClient({}), cache=cache,
                     now=dt.datetime(2026, 7, 26, 12, 3)).plan()
    assert [t.deep_link for t in cached] == [expected]


# --------------------------- per-ticket share links ---------------------------

import datetime as _dt2
from aviasales_search.planner import build_ticket_share_url


def _leg_local(origin, destination, dep_iso, arr_iso, carrier="EY"):
    dep = _dt2.datetime.fromisoformat(dep_iso)
    arr = _dt2.datetime.fromisoformat(arr_iso)
    return FlightLeg(origin=origin, destination=destination, departure=dep, arrival=arr,
                     carrier=carrier, flight_number="1", departure_ts=0, arrival_ts=0,
                     carrier_name="Etihad Airways")


def _etihad_ticket():
    d0 = DirectionResult(legs=[
        _leg_local("SVO", "AUH", "2026-09-15T23:55", "2026-09-16T06:40"),
        _leg_local("AUH", "DPS", "2026-09-16T09:55", "2026-09-16T23:30"),
    ])
    d1 = DirectionResult(legs=[
        _leg_local("DPS", "AUH", "2026-12-15T01:30", "2026-12-15T06:25"),
        _leg_local("AUH", "SVO", "2026-12-15T08:25", "2026-12-15T13:30"),
    ])
    return Ticket(price_rub=71586, directions=[d0, d1], has_baggage=False,
                  deep_link="", signature="86cf37d8f2f037aa6b91238bd3290023")


def test_build_ticket_share_url_matches_live_verified_example():
    # Live-verified in browser: this URL auto-opens exactly this ticket's card.
    # The 6 mid-segment digits are a non-identifying flag (aviasales matches by
    # signature+airports); we emit 000000, browser-confirmed equivalent.
    url = build_ticket_share_url(
        [("MOW", "DPS", "2026-09-15"), ("DPS", "MOW", "2026-12-15")],
        Passengers(adults=1),
        _etihad_ticket(),
    )
    assert url == (
        "https://www.aviasales.ru/search/MOW1509DPS15121"
        "?t=EY17895165001789601400000000SVOAUHDPS"
        "17972982001797341400000000DPSAUHSVO"
        "_86cf37d8f2f037aa6b91238bd3290023_71586"
    )


def test_build_ticket_share_url_without_signature_falls_back_to_search_page():
    t = _etihad_ticket()
    t.signature = ""
    url = build_ticket_share_url(
        [("MOW", "DPS", "2026-09-15"), ("DPS", "MOW", "2026-12-15")],
        Passengers(adults=1), t,
    )
    assert url == "https://www.aviasales.ru/search/MOW1509DPS15121"
    assert "?t=" not in url


def test_build_ticket_share_url_unsupported_shape_returns_empty():
    t = _etihad_ticket()
    three = [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08"),
             ("TAS", "MOW", "2026-10-20")]
    assert build_ticket_share_url(three, Passengers(adults=1), t) == ""


def test_plan_populates_per_ticket_share_link(tmp_path):
    directions = [
        _direction_cfg("MOW", "DPS", "2026-09-15", "2026-09-15"),
        _direction_cfg("DPS", "MOW", "2026-12-15", "2026-12-15"),
    ]
    ticket = _etihad_ticket()
    client = FakeClient({("2026-09-15", "2026-12-15"): [ticket]})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    got = Planner(config=_itinerary(directions, max_requests=2), client=client, cache=cache,
                  now=dt.datetime(2026, 7, 27, 12, 0)).plan()
    assert len(got) == 1
    assert got[0].deep_link.endswith("_86cf37d8f2f037aa6b91238bd3290023_71586")
    assert "?t=EY" in got[0].deep_link


# --------------------------- progress reporting ---------------------------


class RecordingReporter:
    """Fake `ProgressReporter`: records start args and emitted events."""

    def __init__(self):
        self.started_with = None
        self.events = []

    def start(self, total, budget, ttl_minutes):
        self.started_with = (total, budget, ttl_minutes)

    def emit(self, event):
        self.events.append(event)


def test_plan_reports_start_and_one_event_per_combo(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-23")]  # 4 days
    itinerary = _itinerary(directions, max_requests=40, date_samples_per_direction=2)
    ticket = _ticket([_direct_direction("MOW", "IST", "2026-08-20")], price=500)
    client = FakeClient({("2026-08-20",): [ticket]})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    reporter = RecordingReporter()
    planner = Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0),
                      progress=reporter)
    planner.plan()

    assert reporter.started_with == (2, 40, itinerary.cache.ttl_minutes)
    assert [e.index for e in reporter.events] == [1, 2]
    assert all(e.total == 2 for e in reporter.events)
    assert all(e.source == "сеть" for e in reporter.events)
    first = reporter.events[0]
    assert first.dated_dirs == [("MOW", "IST", "2026-08-20")]
    assert (first.found, first.passed, first.best_price) == (1, 1, 500)
    second = reporter.events[1]
    assert (second.found, second.passed, second.best_price) == (0, 0, None)


def test_plan_reports_cache_source_on_second_run(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-20")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    client = FakeClient({})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 7, 26, 12, 0)
    Planner(itinerary, client, cache, now=now).plan()

    reporter = RecordingReporter()
    Planner(itinerary, client, cache, now=now, progress=reporter).plan()
    assert [e.source for e in reporter.events] == ["кэш"]


def test_plan_reports_skipped_combos_when_budget_exhausted(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-23")]  # 4 days
    itinerary = _itinerary(directions, max_requests=1, date_samples_per_direction=2)
    client = FakeClient({})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    reporter = RecordingReporter()
    Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0),
            progress=reporter).plan()

    assert [e.source for e in reporter.events] == ["сеть", "пропуск"]
    skipped = reporter.events[1]
    assert (skipped.found, skipped.passed, skipped.best_price) == (None, None, None)


def test_plan_without_reporter_stays_silent(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-20")]
    itinerary = _itinerary(directions, date_samples_per_direction=1)
    client = FakeClient({})
    cache = ProbeCache(tmp_path / "probes.jsonl")
    Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0)).plan()


def test_plan_reports_running_min_and_improvement_flags(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-22")]  # 3 days
    itinerary = _itinerary(directions, date_samples_per_direction=3)
    tickets_by_dates = {
        ("2026-08-20",): [_ticket([_direct_direction("MOW", "IST", "2026-08-20")], price=500)],
        ("2026-08-22",): [_ticket([_direct_direction("MOW", "IST", "2026-08-22")], price=400)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    reporter = RecordingReporter()
    Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0),
            progress=reporter).plan()

    assert [e.running_min for e in reporter.events] == [500, 500, 400]
    assert [e.improved for e in reporter.events] == [True, False, True]


def test_plan_skipped_combo_carries_previous_running_min(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-21")]  # 2 days
    itinerary = _itinerary(directions, max_requests=1, date_samples_per_direction=2)
    tickets_by_dates = {
        ("2026-08-20",): [_ticket([_direct_direction("MOW", "IST", "2026-08-20")], price=700)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    reporter = RecordingReporter()
    Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0),
            progress=reporter).plan()

    assert [e.source for e in reporter.events] == ["сеть", "пропуск"]
    skipped = reporter.events[1]
    assert (skipped.running_min, skipped.improved) == (700, False)


def test_plan_calls_live_report_with_cumulative_sorted_tickets(tmp_path):
    directions = [_direction_cfg("MOW", "IST", "2026-08-20", "2026-08-22")]  # 3 days
    itinerary = _itinerary(directions, date_samples_per_direction=3)
    tickets_by_dates = {
        ("2026-08-20",): [_ticket([_direct_direction("MOW", "IST", "2026-08-20")], price=500)],
        ("2026-08-22",): [_ticket([_direct_direction("MOW", "IST", "2026-08-22")], price=400)],
    }
    client = FakeClient(tickets_by_dates)
    cache = ProbeCache(tmp_path / "probes.jsonl")
    snapshots = []
    Planner(itinerary, client, cache, now=dt.datetime(2026, 7, 26, 12, 0),
            live_report=lambda tickets: snapshots.append([t.price_rub for t in tickets])).plan()

    # день 2026-08-21 без билетов -> колбэка нет; списки накопительные и по цене
    assert snapshots == [[500], [400, 500]]
