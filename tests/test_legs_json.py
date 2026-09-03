import datetime as dt

from aviasales_search.legs_json import LEGS_SCHEMA_VERSION, legs_to_json
from aviasales_search.leg_sweep import LegDate, LegPrices
from aviasales_search.ranking import RankedCombo
from aviasales_search.trip_model import (
    CacheCfg, Constraints, DateWindow, Direction, DirectionResult, FlightLeg,
    Itinerary, Passengers, SearchBudget, Ticket,
)


def _ts(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _one_way(o, d, date_iso, price):
    dep = dt.datetime.fromisoformat(f"{date_iso}T10:00:00")
    arr = dep + dt.timedelta(hours=3)
    leg = FlightLeg(o, d, dep, arr, "TK", "TK1", _ts(dep), _ts(arr), carrier_name="Turkish")
    return Ticket(price_rub=price, directions=[DirectionResult(legs=[leg])],
                  has_baggage=True, deep_link="link", signature="s", baggage_weight_kg=20)


def _config():
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y", passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 10))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 16), dt.date(2026, 9, 16))),
        ],
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints(), Constraints()],
        search_budget=SearchBudget(), cache=CacheCfg(),
    )


def test_legs_to_json_shape():
    cfg = _config()
    d0, d1 = dt.date(2026, 9, 10), dt.date(2026, 9, 16)
    prices = LegPrices(per_direction=[
        {d0: LegDate(d0, [_one_way("MOW", "ALA", "2026-09-10", 50972)])},
        {d1: LegDate(d1, [_one_way("ALA", "TAS", "2026-09-16", 20000)])},
    ])
    ranked = [RankedCombo(combo=(d0, d1), per_leg_min=(50972, 20000), total=70972)]
    data = legs_to_json(cfg, prices, ranked, trip={"x": 1},
                        generated_at=dt.datetime(2026, 8, 8, 12, 0, 0))

    assert data["schema_version"] == LEGS_SCHEMA_VERSION
    assert data["legs"][0]["route"] == "MOW→ALA"
    assert data["legs"][0]["dates"][0]["min_price_rub"] == 50972
    assert data["legs"][0]["dates"][0]["carrier_name"] == "Turkish"
    assert data["ranking"][0]["total_rub"] == 70972
    assert data["ranking"][0]["stay_days"] == [6]
    assert data["ranking"][0]["combo"] == ["2026-09-10", "2026-09-16"]


def test_legs_to_json_emits_null_for_swept_empty_dates():
    """Дата в окне направления без прошедших билетов -> min_price_rub null
    (в MD рендерится «—»); полное окно выводится всегда."""
    cfg = Itinerary(
        currency="rub", market_code="ru", trip_class="Y", passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 12))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 16), dt.date(2026, 9, 16))),
        ],
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints(), Constraints()],
        search_budget=SearchBudget(), cache=CacheCfg(),
    )
    d10, d16 = dt.date(2026, 9, 10), dt.date(2026, 9, 16)
    # у плеча 0 есть билет только на 10.09; 11.09 и 12.09 свипнуты, но пусты
    prices = LegPrices(per_direction=[
        {d10: LegDate(d10, [_one_way("MOW", "ALA", "2026-09-10", 50000)])},
        {d16: LegDate(d16, [_one_way("ALA", "TAS", "2026-09-16", 20000)])},
    ])
    data = legs_to_json(cfg, prices, [], trip={}, generated_at=dt.datetime(2026, 8, 8))
    leg0 = data["legs"][0]
    assert [d["date"] for d in leg0["dates"]] == ["2026-09-10", "2026-09-11", "2026-09-12"]
    assert leg0["dates"][0]["min_price_rub"] == 50000
    assert leg0["dates"][1]["min_price_rub"] is None  # 11.09 пусто -> null
    assert leg0["dates"][2]["min_price_rub"] is None  # 12.09 пусто -> null
