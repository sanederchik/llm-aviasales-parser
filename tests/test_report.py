import datetime as dt

from aviasales_search.report import (
    itinerary_to_offer_dict,
    offers_sorted_desc,
    render_markdown,
)
from aviasales_search.trip_model import DirectionResult, FlightLeg, Ticket

# --------------------------- builders ---------------------------


def _ts(d: dt.datetime) -> int:
    """Treat naive test datetimes as UTC so timestamp diffs equal naive deltas
    exactly, independent of the machine's local timezone/DST."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="SU", flight="SU1"):
    return FlightLeg(
        origin=origin, destination=destination, departure=dep, arrival=arr,
        carrier=carrier, flight_number=flight,
        departure_ts=_ts(dep), arrival_ts=_ts(arr),
    )


def _direct_direction(origin, destination, date_iso, dep_hour=10, duration_hours=3,
                      carrier="SU"):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr = dep + dt.timedelta(hours=duration_hours)
    return DirectionResult(legs=[_leg(origin, destination, dep, arr, carrier=carrier)])


def _transfer_direction(origin, hub, destination, date_iso, gap_minutes=120,
                        leg_hours=3, dep_hour=10, carrier="SU"):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr1 = dep + dt.timedelta(hours=leg_hours)
    dep2 = arr1 + dt.timedelta(minutes=gap_minutes)
    arr2 = dep2 + dt.timedelta(hours=leg_hours)
    return DirectionResult(legs=[
        _leg(origin, hub, dep, arr1, carrier=carrier),
        _leg(hub, destination, dep2, arr2, carrier=carrier),
    ])


def _ticket(directions, price=1000, has_baggage=True, deep_link="https://aviasales.ru/x"):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage,
                 deep_link=deep_link)


def _roundtrip_ticket(price, date_out="2026-09-15", date_back="2026-12-15"):
    return _ticket([
        _direct_direction("MOW", "DPS", date_out),
        _direct_direction("DPS", "MOW", date_back),
    ], price=price)


# --------------------------- itinerary_to_offer_dict ---------------------------


def test_itinerary_to_offer_dict_has_price_and_route():
    t = _roundtrip_ticket(160423)
    d = itinerary_to_offer_dict(t)
    assert d["price_rub"] == 160423
    assert d["route"] == "MOW→DPS ⇄ DPS→MOW"


# --------------------------- offers_sorted_desc ---------------------------


def test_offers_sorted_desc():
    offers = offers_sorted_desc([
        _roundtrip_ticket(100), _roundtrip_ticket(300), _roundtrip_ticket(200),
    ])
    assert [o["price_rub"] for o in offers] == [300, 200, 100]


def test_offers_sorted_desc_empty():
    assert offers_sorted_desc([]) == []


# --------------------------- render_markdown ---------------------------


def test_render_markdown_shows_cheapest_first_and_limits_top_n():
    md = render_markdown(
        [_roundtrip_ticket(300), _roundtrip_ticket(100), _roundtrip_ticket(200)], top_n=2,
    )
    assert "100" in md
    assert "200" in md
    assert "300" not in md                      # обрезано до топ-2 самых дешёвых
    assert md.index("100") < md.index("200")     # дешёвый идёт раньше


def test_render_markdown_empty_list():
    md = render_markdown([])
    assert "не найдено" in md.lower()


def test_render_markdown_direction_table_columns():
    t = _transfer_direction  # noqa: F841 (kept for readability of intent below)
    ticket = _ticket([
        _transfer_direction("SVO", "CAN", "DPS", "2026-09-15", dep_hour=21,
                            gap_minutes=145, leg_hours=10, carrier="CZ"),
    ], price=160423)
    md = render_markdown([ticket])
    assert "Дата" in md
    assert "Маршрут" in md
    assert "Пересадка" in md
    assert "В пути" in md
    assert "Перевозчик" in md
    assert "SVO→DPS" in md
    assert "CAN" in md          # transfer airport shown
    assert "CZ" in md           # carrier shown


def test_render_markdown_direct_flight_shows_no_transfer_marker():
    ticket = _roundtrip_ticket(100000)
    md = render_markdown([ticket])
    # a direct direction has no transfer airport; must not silently show blank cells
    # as if data were missing - a dash marks "no transfer" explicitly.
    assert "—" in md


def test_render_markdown_formats_price_with_space_separator():
    ticket = _roundtrip_ticket(160423)
    md = render_markdown([ticket])
    assert "160 423" in md


def test_render_markdown_includes_deep_link():
    ticket = _roundtrip_ticket(100000, )
    md = render_markdown([ticket])
    assert "https://aviasales.ru/x" in md


def test_render_markdown_price_delta_shows_cheaper():
    md = render_markdown([_roundtrip_ticket(150)], previous_offers=[{"price_rub": 200}])
    assert "было 200" in md or "−50" in md or "-50" in md


def test_render_markdown_price_delta_shows_more_expensive():
    md = render_markdown([_roundtrip_ticket(250)], previous_offers=[{"price_rub": 200}])
    assert "было 200" in md
    assert "+50" in md


def test_render_markdown_no_delta_section_without_previous_offers():
    md = render_markdown([_roundtrip_ticket(150)])
    assert "было" not in md
