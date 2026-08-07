import datetime as dt

from aviasales_search.filters import apply_filters, passes, passes_direction, passes_itinerary
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
    TimeOfDay,
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


def _direct_direction(origin="MOW", destination="IST", dep_hour=10, duration_hours=3, day=25):
    dep = dt.datetime(2026, 8, day, dep_hour, 0)
    arr = dep + dt.timedelta(hours=duration_hours)
    return DirectionResult(legs=[_leg(origin, destination, dep, arr)])


def _transfer_direction(origin="MOW", hub="AUH", destination="DPS", dep_hour=10,
                         gap_minutes=120, day=25, leg_hours=3):
    dep = dt.datetime(2026, 8, day, dep_hour, 0)
    arr1 = dep + dt.timedelta(hours=leg_hours)
    dep2 = arr1 + dt.timedelta(minutes=gap_minutes)
    arr2 = dep2 + dt.timedelta(hours=leg_hours)
    return DirectionResult(legs=[
        _leg(origin, hub, dep, arr1),
        _leg(hub, destination, dep2, arr2),
    ])


def _ticket(directions, has_baggage=True, price=1000):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage, deep_link="x")


def make_ticket(
    *,
    legs=None,
    depart=None,
    first_leg_origin=None,
    destination="IST",
    price_rub=1000,
    has_baggage=True,
    baggage_weight_kg=None,
    agent_id=None,
    changeable=None,
    refundable=None,
):
    """Фабрика билетов для тестов новых клиентских проверок (задачи 10/10b):
    один DirectionResult, опционально многоплечий (legs=[(origin, dest), ...]).
    Соседние плечи разнесены по времени (гэп 2ч), так что при разных
    origin/dest соседних плеч срабатывает проверка no_airport_change."""
    dep = depart or dt.datetime(2026, 9, 11, 10, 0)
    if legs is None:
        legs = [(first_leg_origin or "MOW", destination)]
    built_legs = []
    cur_dep = dep
    for origin, dest in legs:
        arr = cur_dep + dt.timedelta(hours=3)
        built_legs.append(_leg(origin, dest, cur_dep, arr))
        cur_dep = arr + dt.timedelta(hours=2)
    return Ticket(
        price_rub=price_rub,
        directions=[DirectionResult(legs=built_legs)],
        has_baggage=has_baggage,
        deep_link="x",
        baggage_weight_kg=baggage_weight_kg,
        agent_id=agent_id,
        changeable=changeable,
        refundable=refundable,
    )


def make_itinerary_with_airports(direction_index, from_airports=None, to_airports=None, n=1):
    """Локальный хелпер (обычная функция, не pytest-фикстура): Itinerary, у
    которого direction[direction_index] несёт заданные from_airports/
    to_airports (задача 10b, проверка направляется через passes_itinerary)."""
    dw = DateWindow(earliest=dt.date(2026, 9, 11), latest=dt.date(2026, 9, 11))
    directions = [
        Direction(
            origin="MOW", destination="IST", date_window=dw,
            from_airports=from_airports if i == direction_index else None,
            to_airports=to_airports if i == direction_index else None,
        )
        for i in range(n)
    ]
    return Itinerary(
        currency="rub",
        market_code="ru",
        trip_class="Y",
        passengers=Passengers(adults=1),
        directions=directions,
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints() for _ in range(n)],
        search_budget=SearchBudget(),
        cache=CacheCfg(),
    )


def _itinerary(global_c=None, per_direction_cs=None, n=2):
    dw = DateWindow(earliest=dt.date(2026, 8, 25), latest=dt.date(2026, 8, 25))
    directions = [Direction(origin="MOW", destination="IST", date_window=dw) for _ in range(n)]
    if per_direction_cs is None:
        per_direction_cs = [Constraints() for _ in range(n)]
    return Itinerary(
        currency="rub",
        market_code="ru",
        trip_class="Y",
        passengers=Passengers(adults=1),
        directions=directions,
        global_constraints=global_c if global_c is not None else Constraints(),
        per_direction_constraints=per_direction_cs,
        search_budget=SearchBudget(),
        cache=CacheCfg(),
    )


# --------------------------- passes_direction ---------------------------


def test_passes_direction_max_transfers():
    c = Constraints(max_transfers=0)
    assert passes_direction(_direct_direction(), c)
    assert not passes_direction(_transfer_direction(), c)


def test_passes_direction_max_transfer_minutes():
    c = Constraints(max_transfer_minutes=180)
    assert passes_direction(_transfer_direction(gap_minutes=120), c)
    assert not passes_direction(_transfer_direction(gap_minutes=600), c)


def test_passes_direction_max_duration_minutes():
    dr = _direct_direction(dep_hour=10, duration_hours=3)  # 180 minutes total
    assert passes_direction(dr, Constraints(max_duration_minutes=200))
    assert not passes_direction(dr, Constraints(max_duration_minutes=100))


def test_passes_direction_depart_time_of_day():
    assert passes_direction(_direct_direction(dep_hour=8), Constraints(depart_time_of_day=[TimeOfDay.MORNING]))
    assert not passes_direction(_direct_direction(dep_hour=20), Constraints(depart_time_of_day=[TimeOfDay.MORNING]))


def test_passes_direction_arrive_time_of_day():
    # dep 17:00 + 3h -> arrives 20:00 (EVENING)
    dr = _direct_direction(dep_hour=17, duration_hours=3)
    assert passes_direction(dr, Constraints(arrive_time_of_day=[TimeOfDay.EVENING]))
    assert not passes_direction(dr, Constraints(arrive_time_of_day=[TimeOfDay.MORNING]))


def test_passes_direction_exclude_transfer_airports_auh_cut_pvg_passes():
    c = Constraints(exclude_transfer_airports={"DXB", "AUH", "DOH"})
    assert not passes_direction(_transfer_direction(hub="AUH"), c)
    assert passes_direction(_transfer_direction(hub="PVG"), c)


def test_passes_direction_none_constraints_allow_everything():
    assert passes_direction(_transfer_direction(hub="AUH", gap_minutes=600), Constraints())


def test_passes_direction_airlines_whitelist_all_legs_match():
    c = Constraints(airlines=["TK", "EK"])
    dr = DirectionResult(legs=[
        _leg("MOW", "AUH", dt.datetime(2026, 8, 25, 10), dt.datetime(2026, 8, 25, 13), carrier="TK"),
        _leg("AUH", "DPS", dt.datetime(2026, 8, 25, 15), dt.datetime(2026, 8, 25, 18), carrier="EK"),
    ])
    assert passes_direction(dr, c)


def test_passes_direction_airlines_whitelist_one_leg_outside():
    c = Constraints(airlines=["TK"])
    dr = DirectionResult(legs=[
        _leg("MOW", "AUH", dt.datetime(2026, 8, 25, 10), dt.datetime(2026, 8, 25, 13), carrier="TK"),
        _leg("AUH", "DPS", dt.datetime(2026, 8, 25, 15), dt.datetime(2026, 8, 25, 18), carrier="EK"),
    ])
    assert not passes_direction(dr, c)


def test_passes_direction_airlines_none_does_not_restrict():
    assert passes_direction(_direct_direction(), Constraints(airlines=None))


def test_passes_direction_empty_time_of_day_allows_all():
    dr = _direct_direction(dep_hour=3)
    assert passes_direction(dr, Constraints(depart_time_of_day=[]))


# --------------------------- passes (single Constraints, whole ticket) ---------------------------


def test_passes_baggage_required():
    ticket_with_bag = _ticket([_direct_direction(), _direct_direction()], has_baggage=True)
    ticket_no_bag = _ticket([_direct_direction(), _direct_direction()], has_baggage=False)
    c = Constraints(baggage_required=True)
    assert passes(ticket_with_bag, c)
    assert not passes(ticket_no_bag, c)


def test_passes_baggage_required_false_or_none_allows_no_baggage():
    ticket_no_bag = _ticket([_direct_direction()], has_baggage=False)
    assert passes(ticket_no_bag, Constraints(baggage_required=False))
    assert passes(ticket_no_bag, Constraints())


def test_passes_checks_all_directions_max_transfers():
    c = Constraints(max_transfers=0)
    ok = _ticket([_direct_direction(), _direct_direction()])
    bad_second = _ticket([_direct_direction(), _transfer_direction(hub="PVG")])
    assert passes(ok, c)
    assert not passes(bad_second, c)


def test_passes_exclude_transfer_airports_auh_cut_pvg_passes():
    c = Constraints(exclude_transfer_airports={"DXB", "AUH", "DOH"})
    ticket_auh = _ticket([_direct_direction(), _transfer_direction(hub="AUH")])
    ticket_pvg = _ticket([_direct_direction(), _transfer_direction(hub="PVG")])
    assert not passes(ticket_auh, c)
    assert passes(ticket_pvg, c)


def test_passes_max_transfer_minutes():
    c = Constraints(max_transfer_minutes=180)
    ok = _ticket([_transfer_direction(gap_minutes=120), _transfer_direction(gap_minutes=120)])
    bad = _ticket([_transfer_direction(gap_minutes=120), _transfer_direction(gap_minutes=600)])
    assert passes(ok, c)
    assert not passes(bad, c)


def test_passes_max_duration_minutes():
    c = Constraints(max_duration_minutes=200)
    ok = _ticket([_direct_direction(duration_hours=3), _direct_direction(duration_hours=3, day=26)])
    bad = _ticket([_direct_direction(duration_hours=3), _direct_direction(duration_hours=4, day=26)])
    assert passes(ok, c)
    assert not passes(bad, c)


def test_passes_depart_time_of_day_applies_only_to_first_direction():
    c = Constraints(depart_time_of_day=[TimeOfDay.MORNING])
    # first direction departs 08:00 (morning, ok); second departs 20:00 (evening, irrelevant)
    ticket = _ticket([
        _direct_direction(dep_hour=8, day=25),
        _direct_direction(dep_hour=20, day=26),
    ])
    assert passes(ticket, c)

    # first direction itself violates -> whole ticket fails
    bad_ticket = _ticket([
        _direct_direction(dep_hour=20, day=25),
        _direct_direction(dep_hour=8, day=26),
    ])
    assert not passes(bad_ticket, c)


def test_passes_arrive_time_of_day_applies_only_to_last_direction():
    c = Constraints(arrive_time_of_day=[TimeOfDay.EVENING])
    # last direction: dep 17:00 + 3h -> arrives 20:00 (evening, ok); first direction arrives
    # 13:00 (afternoon) which would violate EVENING if (wrongly) checked.
    ticket = _ticket([
        _direct_direction(dep_hour=10, duration_hours=3, day=25),  # arrives 13:00
        _direct_direction(dep_hour=17, duration_hours=3, day=26),  # arrives 20:00
    ])
    assert passes(ticket, c)

    bad_ticket = _ticket([
        _direct_direction(dep_hour=10, duration_hours=3, day=25),
        _direct_direction(dep_hour=10, duration_hours=3, day=26),  # arrives 13:00 -> fails
    ])
    assert not passes(bad_ticket, c)


def test_passes_none_constraints_allow_everything():
    ticket = _ticket([
        _transfer_direction(hub="AUH", gap_minutes=600),
        _transfer_direction(hub="AUH", gap_minutes=600),
    ], has_baggage=False)
    assert passes(ticket, Constraints())


# --------------------------- passes_itinerary ---------------------------


def test_passes_itinerary_per_direction_constraint_differences():
    itinerary = _itinerary(
        global_c=Constraints(),
        per_direction_cs=[Constraints(max_transfers=0), Constraints(max_transfers=1)],
    )
    ok = _ticket([_direct_direction(), _transfer_direction(hub="PVG")])
    assert passes_itinerary(ok, itinerary)

    # direction 0 exceeds its own (stricter) max_transfers=0
    bad = _ticket([_transfer_direction(hub="PVG"), _transfer_direction(hub="PVG")])
    assert not passes_itinerary(bad, itinerary)


def test_passes_itinerary_exclude_transfer_airports_auh_cut_pvg_passes():
    itinerary = _itinerary(global_c=Constraints(exclude_transfer_airports={"DXB", "AUH", "DOH"}))
    ticket_auh = _ticket([_direct_direction(), _transfer_direction(hub="AUH")])
    ticket_pvg = _ticket([_direct_direction(), _transfer_direction(hub="PVG")])
    assert not passes_itinerary(ticket_auh, itinerary)
    assert passes_itinerary(ticket_pvg, itinerary)


def test_passes_itinerary_length_mismatch_fails_defensively():
    itinerary = _itinerary(n=2)
    ticket_one_direction = _ticket([_direct_direction()])
    assert not passes_itinerary(ticket_one_direction, itinerary)


def test_passes_itinerary_baggage_judged_by_global_only():
    # per-direction constraints try to relax baggage; global requires it -> must still fail
    itinerary = _itinerary(
        global_c=Constraints(baggage_required=True),
        per_direction_cs=[Constraints(baggage_required=False), Constraints(baggage_required=False)],
    )
    ticket_no_bag = _ticket([_direct_direction(), _direct_direction()], has_baggage=False)
    ticket_with_bag = _ticket([_direct_direction(), _direct_direction()], has_baggage=True)
    assert not passes_itinerary(ticket_no_bag, itinerary)
    assert passes_itinerary(ticket_with_bag, itinerary)


def test_passes_itinerary_uses_max_per_direction_baggage_weight_over_directions():
    """Regression (final review, finding 1): baggage_min_weight_kg set ONLY on
    one direction (global_constraints carries no weight threshold at all) must
    still be enforced at the ticket level by passes_itinerary -- the fare is
    one for the whole ticket, so the strictest per-direction threshold must
    apply everywhere, same semantics as planner._min_baggage_weight."""
    itinerary = _itinerary(
        global_c=Constraints(),
        per_direction_cs=[Constraints(), Constraints(baggage_min_weight_kg=20)],
    )
    light_ticket = _ticket(
        [_direct_direction(), _direct_direction()], has_baggage=True,
    )
    light_ticket.baggage_weight_kg = 10
    assert not passes_itinerary(light_ticket, itinerary)

    heavy_ticket = _ticket(
        [_direct_direction(), _direct_direction()], has_baggage=True,
    )
    heavy_ticket.baggage_weight_kg = 25
    assert passes_itinerary(heavy_ticket, itinerary)


def test_passes_itinerary_depart_time_of_day_uses_first_direction_effective_constraints():
    itinerary = _itinerary(
        per_direction_cs=[
            Constraints(depart_time_of_day=[TimeOfDay.MORNING]),
            Constraints(depart_time_of_day=[TimeOfDay.MORNING]),  # must be ignored (not first)
        ],
    )
    # second direction departs in the evening but its own depart_time_of_day must be ignored
    ok = _ticket([
        _direct_direction(dep_hour=8, day=25),
        _direct_direction(dep_hour=20, day=26),
    ])
    assert passes_itinerary(ok, itinerary)

    bad = _ticket([
        _direct_direction(dep_hour=20, day=25),  # first direction itself violates
        _direct_direction(dep_hour=8, day=26),
    ])
    assert not passes_itinerary(bad, itinerary)


def test_passes_itinerary_arrive_time_of_day_uses_last_direction_effective_constraints():
    itinerary = _itinerary(
        per_direction_cs=[
            Constraints(arrive_time_of_day=[TimeOfDay.EVENING]),  # must be ignored (not last)
            Constraints(arrive_time_of_day=[TimeOfDay.EVENING]),
        ],
    )
    ok = _ticket([
        _direct_direction(dep_hour=10, duration_hours=3, day=25),  # arrives 13:00, ignored
        _direct_direction(dep_hour=17, duration_hours=3, day=26),  # arrives 20:00, evening ok
    ])
    assert passes_itinerary(ok, itinerary)

    bad = _ticket([
        _direct_direction(dep_hour=10, duration_hours=3, day=25),
        _direct_direction(dep_hour=10, duration_hours=3, day=26),  # arrives 13:00, fails
    ])
    assert not passes_itinerary(bad, itinerary)


def test_passes_itinerary_max_transfer_minutes_and_duration_per_direction():
    itinerary = _itinerary(
        per_direction_cs=[
            Constraints(max_transfer_minutes=180, max_duration_minutes=1000),
            Constraints(max_transfer_minutes=180, max_duration_minutes=1000),
        ],
    )
    ok = _ticket([_transfer_direction(gap_minutes=120), _transfer_direction(gap_minutes=120)])
    bad_gap = _ticket([_transfer_direction(gap_minutes=600), _transfer_direction(gap_minutes=120)])
    assert passes_itinerary(ok, itinerary)
    assert not passes_itinerary(bad_gap, itinerary)


# --------------------------- apply_filters ---------------------------


def test_apply_filters_preserves_order_and_filters():
    c = Constraints(max_transfers=0)
    tickets = [
        _ticket([_direct_direction()]),
        _ticket([_transfer_direction(hub="PVG")]),
        _ticket([_direct_direction()]),
    ]
    result = apply_filters(tickets, c)
    assert result == [tickets[0], tickets[2]]


def test_apply_filters_none_constraints_returns_all():
    tickets = [_ticket([_direct_direction()]), _ticket([_transfer_direction(hub="AUH")])]
    assert apply_filters(tickets, Constraints()) == tickets


# --------------------------- Task 10: вес багажа ---------------------------


def test_min_baggage_weight_filters_out_light_ticket():
    t = make_ticket(baggage_weight_kg=10)
    c = Constraints(baggage_required=True, baggage_min_weight_kg=20)
    assert passes(t, c) is False


def test_unknown_weight_fails_when_threshold_set():
    t = make_ticket(baggage_weight_kg=None)
    c = Constraints(baggage_min_weight_kg=20)
    assert passes(t, c) is False


def test_sufficient_baggage_weight_passes():
    t = make_ticket(baggage_weight_kg=25)
    c = Constraints(baggage_required=True, baggage_min_weight_kg=20)
    assert passes(t, c) is True


# --------------------------- Task 10b: остальные новые поля ---------------------------


def test_max_price_filters_out_expensive():
    t = make_ticket(price_rub=300000)
    assert passes(t, Constraints(max_price=250000)) is False


def test_max_price_boundary_inclusive():
    t = make_ticket(price_rub=250000)
    assert passes(t, Constraints(max_price=250000)) is True


def test_exact_depart_time_range():
    t = make_ticket(depart=dt.datetime(2026, 9, 11, 5, 30))
    c = Constraints(depart_time=(dt.time(6, 0), dt.time(12, 0)))
    assert passes(t, c) is False


def test_exact_depart_time_range_inside_passes():
    t = make_ticket(depart=dt.datetime(2026, 9, 11, 8, 0))
    c = Constraints(depart_time=(dt.time(6, 0), dt.time(12, 0)))
    assert passes(t, c) is True


def test_no_airport_change_rejects_cross_airport_transfer():
    t = make_ticket(legs=[("ALA", "DMK"), ("BKK", "DPS")])   # прилёт DMK, вылет BKK
    assert passes(t, Constraints(no_airport_change=True)) is False


def test_no_airport_change_allows_same_airport_transfer():
    t = make_ticket(legs=[("ALA", "DMK"), ("DMK", "DPS")])
    assert passes(t, Constraints(no_airport_change=True)) is True


def test_agents_whitelist():
    t = make_ticket(agent_id=183)
    assert passes(t, Constraints(agents=["kupibilet|70"])) is False
    assert passes(t, Constraints(agents=["aviakassa|183"])) is True


def test_agents_unknown_agent_id_fails_when_filter_set():
    t = make_ticket(agent_id=None)
    assert passes(t, Constraints(agents=["aviakassa|183"])) is False


def test_changeable_only():
    assert passes(make_ticket(changeable=False), Constraints(changeable_only=True)) is False
    assert passes(make_ticket(changeable=None), Constraints(changeable_only=True)) is False
    assert passes(make_ticket(changeable=True), Constraints(changeable_only=True)) is True


def test_refundable_only():
    t = make_ticket(refundable=False)
    assert passes(t, Constraints(refundable_only=True)) is False


def test_direction_airports_checked_in_itinerary():
    # itinerary с directions[0].from_airports=["DME"]; билет вылетает из VKO → False
    itinerary = make_itinerary_with_airports(0, from_airports=["DME"])
    t = make_ticket(first_leg_origin="VKO")
    assert passes_itinerary(t, itinerary) is False


def test_direction_to_airports_checked_in_itinerary():
    itinerary = make_itinerary_with_airports(0, to_airports=["SVO"])
    t = make_ticket(destination="DME")
    assert passes_itinerary(t, itinerary) is False
    t_ok = make_ticket(destination="SVO")
    assert passes_itinerary(t_ok, itinerary) is True
