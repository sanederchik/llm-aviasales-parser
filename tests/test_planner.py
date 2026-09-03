import datetime as dt

from aviasales_search.planner import (
    build_ticket_share_url,
    search_page_url,
    ticket_from_dict,
    ticket_to_dict,
)
from aviasales_search.trip_model import (
    DirectionResult,
    FlightLeg,
    Passengers,
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


def _ticket(directions, price=1000, has_baggage=True):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage, deep_link="x")


# --------------------------- ticket_to_dict / ticket_from_dict roundtrip ---------------------------


def test_ticket_dict_roundtrip_preserves_ts_and_carrier_name():
    """Regression: departure_ts/arrival_ts (timezone-correct durations) and
    carrier_name (airline display name) must survive a cache put/get roundtrip,
    otherwise duration_minutes and the report's airline name would silently
    revert to defaults (0 / bare IATA code) for any ticket served from cache."""
    leg = FlightLeg(
        origin="SVO", destination="CAN",
        departure=dt.datetime(2026, 9, 15, 21, 15),
        arrival=dt.datetime(2026, 9, 16, 2, 30),
        carrier="CZ", flight_number="625",
        departure_ts=1789499700, arrival_ts=1789529400,
        carrier_name="China Southern Airlines",
    )
    ticket = _ticket([DirectionResult(legs=[leg])], price=160423)

    restored = ticket_from_dict(ticket_to_dict(ticket))

    restored_leg = restored.directions[0].legs[0]
    assert restored_leg.departure_ts == 1789499700
    assert restored_leg.arrival_ts == 1789529400
    assert restored_leg.carrier_name == "China Southern Airlines"
    assert restored.directions[0].duration_minutes == ticket.directions[0].duration_minutes
    assert restored.directions[0].main_carrier_name == "China Southern Airlines"


def test_ticket_dict_roundtrip_preserves_baggage_weight_agent_and_flags():
    ticket = _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)
    ticket.baggage_weight_kg = 20
    ticket.agent_id = 183
    ticket.changeable = True
    ticket.refundable = False

    restored = ticket_from_dict(ticket_to_dict(ticket))

    assert restored.baggage_weight_kg == 20
    assert restored.agent_id == 183
    assert restored.changeable is True
    assert restored.refundable is False


def test_ticket_from_dict_defaults_new_fields_to_none_for_old_cache_records():
    """Записи кэша до Task 9 не содержат новых полей -> обратная
    совместимость: None, а не KeyError."""
    ticket = _ticket([_direct_direction("MOW", "IST", "2026-08-25")], price=150)
    legacy = ticket_to_dict(ticket)
    del legacy["baggage_weight_kg"], legacy["agent_id"], legacy["changeable"], legacy["refundable"]

    restored = ticket_from_dict(legacy)

    assert restored.baggage_weight_kg is None
    assert restored.agent_id is None
    assert restored.changeable is None
    assert restored.refundable is None


# --------------------------- search_page_url ---------------------------


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


def test_search_page_url_multicity_three_legs():
    url = search_page_url(
        [("MOW", "IST", "2026-09-13"), ("IST", "DPS", "2026-10-20"),
         ("DPS", "MOW", "2026-11-15")],
        Passengers(adults=1),
    )
    assert url == "https://www.aviasales.ru/search/MOW1309IST2010DPS1511MOW1"


def test_search_page_url_open_jaw_uses_composite_code():
    url = search_page_url(
        [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08")],
        Passengers(adults=1),
    )
    assert url == "https://www.aviasales.ru/search/MOW0110IST0810TAS1"


# --------------------------- per-ticket share links ---------------------------


def _leg_local(origin, destination, dep_iso, arr_iso, carrier="EY"):
    dep = dt.datetime.fromisoformat(dep_iso)
    arr = dt.datetime.fromisoformat(arr_iso)
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


def test_build_ticket_share_url_three_legs_falls_back_to_base_search_page():
    # Мультигород больше не "неподдерживаемая форма" (см. search_page_url), но
    # формат `?t=` для 3+ плеч вживую не проверен — падаем на base search_page_url.
    three = [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08"),
             ("TAS", "MOW", "2026-10-20")]
    ticket = Ticket(
        price_rub=50000,
        directions=[
            DirectionResult(legs=[_leg_local("MOW", "IST", "2026-10-01T10:00",
                                             "2026-10-01T14:00")]),
            DirectionResult(legs=[_leg_local("IST", "TAS", "2026-10-08T10:00",
                                             "2026-10-08T14:00")]),
            DirectionResult(legs=[_leg_local("TAS", "MOW", "2026-10-20T10:00",
                                             "2026-10-20T14:00")]),
        ],
        has_baggage=False, deep_link="", signature="deadbeef",
    )
    url = build_ticket_share_url(three, Passengers(adults=1), ticket)
    assert url == search_page_url(three, Passengers(adults=1))
    assert "?t=" not in url


def test_build_ticket_share_url_open_jaw_two_legs_falls_back_to_base_search_page():
    # Open-jaw из 2 плеч (MOW->IST, IST->TAS) — это НЕ настоящий round-trip
    # (второе плечо не зеркалит первое), поэтому формат `?t=` не верифицирован:
    # должны получить голую search_page_url без `?t=`, даже с непустой signature.
    two = [("MOW", "IST", "2026-10-01"), ("IST", "TAS", "2026-10-08")]
    ticket = Ticket(
        price_rub=50000,
        directions=[
            DirectionResult(legs=[_leg_local("MOW", "IST", "2026-10-01T10:00",
                                             "2026-10-01T14:00")]),
            DirectionResult(legs=[_leg_local("IST", "TAS", "2026-10-08T10:00",
                                             "2026-10-08T14:00")]),
        ],
        has_baggage=False, deep_link="", signature="deadbeef",
    )
    url = build_ticket_share_url(two, Passengers(adults=1), ticket)
    assert url == search_page_url(two, Passengers(adults=1))
    assert "?t=" not in url


def test_build_ticket_share_url_multicity_returns_base_without_t():
    d0 = DirectionResult(legs=[_leg_local("SVO", "IST", "2026-09-13T10:00",
                                          "2026-09-13T14:00")])
    d1 = DirectionResult(legs=[_leg_local("IST", "DPS", "2026-10-20T10:00",
                                          "2026-10-20T23:00")])
    d2 = DirectionResult(legs=[_leg_local("DPS", "SVO", "2026-11-15T01:00",
                                          "2026-11-15T13:00")])
    ticket = Ticket(price_rub=99000, directions=[d0, d1, d2], has_baggage=False,
                    deep_link="", signature="abc123")
    url = build_ticket_share_url(
        [("MOW", "IST", "2026-09-13"), ("IST", "DPS", "2026-10-20"),
         ("DPS", "MOW", "2026-11-15")],
        Passengers(adults=1), ticket,
    )
    assert url == "https://www.aviasales.ru/search/MOW1309IST2010DPS1511MOW1"
    assert "?t=" not in url                      # формат ?t= для 3+ плеч не верифицирован
