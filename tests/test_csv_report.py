import csv
import datetime as dt
import io

from aviasales_search.csv_report import BOM, csv_prefixes, render_csv, route_pairs_from_trip
from aviasales_search.trip_model import DirectionResult, FlightLeg, Ticket

# --------------------------- builders ---------------------------


def _ts(d: dt.datetime) -> int:
    """Naive-даты тестов трактуем как UTC, чтобы разницы ts совпадали с
    naive-дельтами независимо от таймзоны машины."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="SU", flight="SU1", carrier_name="Аэрофлот"):
    return FlightLeg(
        origin=origin, destination=destination, departure=dep, arrival=arr,
        carrier=carrier, flight_number=flight,
        departure_ts=_ts(dep), arrival_ts=_ts(arr), carrier_name=carrier_name,
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


def _ticket(directions, price=1000, has_baggage=True, deep_link="https://aviasales.ru/x",
           baggage_weight_kg=None):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage,
                  deep_link=deep_link, baggage_weight_kg=baggage_weight_kg)


ROUTE_PAIRS = [("MOW", "DPS"), ("DPS", "MOW")]


def _roundtrip_ticket(price, has_baggage=True, baggage_weight_kg=None):
    return _ticket([
        _direct_direction("MOW", "DPS", "2026-09-15"),
        _direct_direction("DPS", "MOW", "2026-12-15"),
    ], price=price, has_baggage=has_baggage, baggage_weight_kg=baggage_weight_kg)


def _parse(rendered: str) -> list[list[str]]:
    assert rendered.startswith(BOM)
    return list(csv.reader(io.StringIO(rendered.lstrip(BOM))))


# --------------------------- prefixes ---------------------------


def test_route_pairs_from_trip():
    trip = {"directions": [{"from": "MOW", "to": "TAS", "date_window": {}},
                           {"from": "TAS", "to": "MOW"}]}
    assert route_pairs_from_trip(trip) == [("MOW", "TAS"), ("TAS", "MOW")]


def test_csv_prefixes_simple():
    assert csv_prefixes([("MOW", "TAS"), ("TAS", "ALA")]) == ["mow_tas", "tas_ala"]


def test_csv_prefixes_deduplicates_repeated_route():
    assert csv_prefixes([("MOW", "DPS"), ("DPS", "MOW"), ("MOW", "DPS")]) == [
        "mow_dps", "dps_mow", "mow_dps_2"]


# --------------------------- header ---------------------------


def test_header_columns_per_direction():
    rows = _parse(render_csv([_roundtrip_ticket(100)], ROUTE_PAIRS))
    assert rows[0] == [
        "цена_руб", "багаж", "багаж_кг",
        "mow_dps_дата", "mow_dps_вылет", "mow_dps_прилет", "mow_dps_длит_мин",
        "mow_dps_авиакомпания", "mow_dps_пересадки", "mow_dps_аэропорты_пересадок",
        "dps_mow_дата", "dps_mow_вылет", "dps_mow_прилет", "dps_mow_длит_мин",
        "dps_mow_авиакомпания", "dps_mow_пересадки", "dps_mow_аэропорты_пересадок",
        "ссылка",
    ]


def test_header_only_when_no_tickets():
    rows = _parse(render_csv([], ROUTE_PAIRS))
    assert len(rows) == 1
    assert rows[0][0] == "цена_руб"


# --------------------------- rows ---------------------------


def test_rows_sorted_by_price_ascending_and_all_tickets_present():
    rows = _parse(render_csv(
        [_roundtrip_ticket(300), _roundtrip_ticket(100), _roundtrip_ticket(200)],
        ROUTE_PAIRS))
    assert [r[0] for r in rows[1:]] == ["100", "200", "300"]


def test_row_cells_direct_flight():
    rows = _parse(render_csv(
        [_roundtrip_ticket(185000, baggage_weight_kg=20)], ROUTE_PAIRS))
    row = rows[1]
    assert row[0] == "185000"
    assert row[1] == "да"
    assert row[2] == "20"                        # багаж_кг: ошибка вида 10 вместо 20 видна
    # Плечо MOW→DPS: вылет 2026-09-15 10:00, прилёт через 3 часа.
    assert row[3] == "2026-09-15"
    assert row[4] == "10:00"
    assert row[5] == "2026-09-15T13:00"
    assert row[6] == "180"
    assert row[7] == "Аэрофлот"
    assert row[8] == "0"
    assert row[9] == ""
    assert row[-1] == "https://aviasales.ru/x"


def test_row_without_baggage():
    rows = _parse(render_csv([_roundtrip_ticket(100, has_baggage=False)], ROUTE_PAIRS))
    assert rows[1][1] == "нет"
    assert rows[1][2] == ""                       # веса нет — пусто, а не "0"


def test_row_baggage_without_known_weight():
    rows = _parse(render_csv([_roundtrip_ticket(100)], ROUTE_PAIRS))
    assert rows[1][1] == "да"
    assert rows[1][2] == ""


def test_transfer_airports_joined_and_quoted():
    t = _ticket([
        DirectionResult(legs=_transfer_direction("MOW", "IST", "DPS", "2026-09-15").legs
                        + _direct_direction("DPS", "MOW", "2026-09-16").legs),
    ], price=100)
    # Направление с двумя пересадками (IST и DPS) — в ячейке "IST,DPS";
    # csv.reader обязан вернуть её как одно поле (модуль csv экранирует сам).
    rows = _parse(render_csv([t], [("MOW", "MOW")]))
    assert rows[1][9] == "IST,DPS"


def test_overnight_arrival_keeps_full_datetime():
    dep = dt.datetime(2026, 11, 25, 20, 0)
    arr = dt.datetime(2026, 11, 26, 6, 30)
    t = _ticket([DirectionResult(legs=[_leg("DPS", "MOW", dep, arr)])], price=100)
    rows = _parse(render_csv([t], [("DPS", "MOW")]))
    assert rows[1][5] == "2026-11-26T06:30"
