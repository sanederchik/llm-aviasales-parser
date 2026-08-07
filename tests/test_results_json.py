import datetime as dt

import pytest

from aviasales_search.results_json import (
    SCHEMA_VERSION,
    ResultsJsonError,
    tickets_from_json,
    tickets_to_json,
)
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


def _ticket(directions, price=1000, has_baggage=True, deep_link="https://aviasales.ru/x"):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage,
                  deep_link=deep_link, signature=f"sig-{price}")


TRIP = {"directions": [{"from": "MOW", "to": "DPS"}, {"from": "DPS", "to": "MOW"}]}
NOW = dt.datetime(2026, 8, 5, 12, 0)


def _roundtrip_ticket(price):
    return _ticket([
        _transfer_direction("MOW", "IST", "DPS", "2026-09-15"),
        _direct_direction("DPS", "MOW", "2026-12-15"),
    ], price=price)


# --------------------------- tickets_to_json ---------------------------


def test_to_json_top_level_schema():
    data = tickets_to_json([_roundtrip_ticket(100)], TRIP, NOW)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["generated_at"] == "2026-08-05T12:00:00"
    assert data["trip"] == TRIP
    assert len(data["offers"]) == 1


def test_to_json_offers_sorted_by_price_ascending():
    data = tickets_to_json(
        [_roundtrip_ticket(300), _roundtrip_ticket(100), _roundtrip_ticket(200)],
        TRIP, NOW)
    assert [o["price_rub"] for o in data["offers"]] == [100, 200, 300]


def test_to_json_direction_fields():
    data = tickets_to_json([_roundtrip_ticket(100)], TRIP, NOW)
    d = data["offers"][0]["directions"][0]
    assert d["route"] == "MOW→DPS"
    assert d["origin"] == "MOW" and d["destination"] == "DPS"
    assert d["depart"] == "2026-09-15T10:00:00"
    assert d["arrive"] == "2026-09-15T18:00:00"
    assert d["duration_minutes"] == 8 * 60
    assert d["transfers"] == 1
    assert d["transfer_airports"] == ["IST"]
    assert d["carrier"] == "SU"
    assert d["carrier_name"] == "Аэрофлот"
    assert len(d["legs"]) == 2
    leg = d["legs"][0]
    assert leg["origin"] == "MOW" and leg["destination"] == "IST"
    assert leg["departure"] == "2026-09-15T10:00:00"
    assert leg["departure_ts"] == _ts(dt.datetime(2026, 9, 15, 10, 0))
    assert leg["flight_number"] == "SU1"


def test_to_json_offer_fields():
    data = tickets_to_json([_roundtrip_ticket(100)], TRIP, NOW)
    o = data["offers"][0]
    assert o["price_rub"] == 100
    assert o["has_baggage"] is True
    assert o["deep_link"] == "https://aviasales.ru/x"
    assert o["route"] == "MOW→DPS ⇄ DPS→MOW"
    assert o["signature"] == "sig-100"


def test_to_json_offer_includes_baggage_weight_agent_and_fare_flags():
    ticket = _roundtrip_ticket(100)
    ticket.baggage_weight_kg = 20
    ticket.agent_id = 183
    ticket.changeable = True
    ticket.refundable = False
    data = tickets_to_json([ticket], TRIP, NOW)
    o = data["offers"][0]
    assert o["baggage_weight_kg"] == 20
    assert o["agent_id"] == 183
    assert o["changeable"] is True
    assert o["refundable"] is False


def test_from_json_defaults_new_fields_to_none_for_legacy_file():
    """Файлы результатов до Task 9 не содержат новых полей -> обратная
    совместимость: None, а не KeyError."""
    data = tickets_to_json([_roundtrip_ticket(100)], TRIP, NOW)
    legacy_offer = dict(data["offers"][0])
    for key in ("baggage_weight_kg", "agent_id", "changeable", "refundable"):
        legacy_offer.pop(key, None)
    data["offers"][0] = legacy_offer

    restored = tickets_from_json(data)

    assert restored[0].baggage_weight_kg is None
    assert restored[0].agent_id is None
    assert restored[0].changeable is None
    assert restored[0].refundable is None


def test_to_json_empty_tickets():
    data = tickets_to_json([], TRIP, NOW)
    assert data["offers"] == []


# --------------------------- round-trip ---------------------------


def test_roundtrip_restores_tickets_exactly():
    original = [_roundtrip_ticket(200), _roundtrip_ticket(100)]
    restored = tickets_from_json(tickets_to_json(original, TRIP, NOW))
    # После сериализации порядок — по возрастанию цены.
    assert restored == sorted(original, key=lambda t: t.price_rub)


def test_roundtrip_preserves_unix_ts_duration():
    t = _ticket([_transfer_direction("ALA", "TAS", "DPS", "2026-10-01")], price=500)
    restored = tickets_from_json(tickets_to_json([t], TRIP, NOW))
    assert restored[0].directions[0].duration_minutes == t.directions[0].duration_minutes


# --------------------------- tickets_from_json errors ---------------------------


def test_from_json_rejects_missing_schema_version():
    with pytest.raises(ResultsJsonError):
        tickets_from_json({"offers": []})


def test_from_json_rejects_unknown_schema_version():
    with pytest.raises(ResultsJsonError):
        tickets_from_json({"schema_version": 99, "offers": []})


def test_from_json_rejects_non_dict():
    with pytest.raises(ResultsJsonError):
        tickets_from_json(["not", "a", "dict"])


def test_from_json_rejects_broken_offer():
    data = tickets_to_json([_roundtrip_ticket(100)], TRIP, NOW)
    del data["offers"][0]["directions"][0]["legs"][0]["departure_ts"]
    with pytest.raises(ResultsJsonError):
        tickets_from_json(data)
