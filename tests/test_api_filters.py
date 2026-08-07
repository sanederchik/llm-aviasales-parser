"""Тесты транслятора Itinerary -> filters_state (aviasales_search/api_filters.py)."""
from __future__ import annotations

import datetime as dt

from aviasales_search.api_filters import build_filters_state
from aviasales_search.trip_model import Constraints, GULF_AIRPORTS, Itinerary, TimeOfDay, parse_config


def make_itinerary(
    *,
    global_constraints: Constraints | None = None,
    per_direction: dict[int, Constraints] | None = None,
    direction_airports: dict[int, tuple[list[str] | None, list[str] | None]] | None = None,
    same_airport_cities: list[str] | None = None,
    num_directions: int | None = None,
) -> Itinerary:
    """Строит минимальный Itinerary поверх parse_config, затем подменяет
    global_constraints/per_direction_constraints готовыми объектами Constraints
    (сборка через сырой JSON-конфиг для них была бы избыточной для этих тестов).

    Число направлений по умолчанию выводится из наибольшего индекса,
    упомянутого в per_direction/direction_airports (минимум 1); можно задать
    явно через num_directions. Города для направлений: MOW -> CITY1 -> CITY2
    -> ... — направление 0 всегда начинается в MOW.
    """
    per_direction = per_direction or {}
    direction_airports = direction_airports or {}
    n = num_directions
    if n is None:
        n = max([0, *per_direction.keys(), *direction_airports.keys()]) + 1
    cities = ["MOW"] + [f"CITY{i}" for i in range(1, n + 1)]

    directions_cfg = []
    for i in range(n):
        d = {
            "from": cities[i],
            "to": cities[i + 1],
            "date_window": {"earliest": "2026-09-01", "latest": "2026-09-01"},
        }
        from_airports, to_airports = direction_airports.get(i, (None, None))
        if from_airports is not None:
            d["from_airports"] = from_airports
        if to_airports is not None:
            d["to_airports"] = to_airports
        directions_cfg.append(d)

    config: dict = {"directions": directions_cfg}
    if same_airport_cities is not None:
        config["same_airport_cities"] = same_airport_cities

    itinerary = parse_config(config)
    if global_constraints is not None:
        itinerary.global_constraints = global_constraints
    for i, c in per_direction.items():
        itinerary.per_direction_constraints[i] = c
    return itinerary


def test_empty_constraints_give_only_sort():
    assert build_filters_state(make_itinerary()) == {"sort": "price_asc"}


def test_baggage_weight_rounds_down_to_allowed_choice():
    it = make_itinerary(global_constraints=Constraints(
        baggage_required=True, baggage_min_weight_kg=25))
    st = build_filters_state(it)
    assert st["baggage"] is True and st["baggage_weight"] == "20"


def test_baggage_weight_below_ten_not_sent():
    it = make_itinerary(global_constraints=Constraints(baggage_min_weight_kg=5))
    st = build_filters_state(it)
    assert st["baggage"] is True and "baggage_weight" not in st


def test_max_transfers_enumerates_counts():
    it = make_itinerary(global_constraints=Constraints(max_transfers=1))
    assert build_filters_state(it)["transfers_count"] == ["0", "1"]


def test_gulf_exclusion_maps_to_flag_custom_stays_client_side():
    it = make_itinerary(global_constraints=Constraints(
        exclude_transfer_airports=set(GULF_AIRPORTS)))
    assert build_filters_state(it)["transfers_without_persian_gulf"] is True
    it2 = make_itinerary(global_constraints=Constraints(exclude_transfer_airports={"IST"}))
    assert "transfers_without_persian_gulf" not in build_filters_state(it2)


def test_ranges_and_scalars():
    it = make_itinerary(global_constraints=Constraints(
        max_duration_minutes=1440, max_transfer_minutes=240, max_price=300000))
    st = build_filters_state(it)
    assert st["trip_duration"] == {"min": 0, "max": 1440}
    assert st["transfers_duration"] == {"min": 0, "max": 240}
    assert st["price"] == {"min": 0, "max": 300000}


def test_bool_and_set_passthrough():
    it = make_itinerary(global_constraints=Constraints(
        airlines=["SU", "KC"], alliances=["1"], agents=["aviasales|1"],
        payment_methods=["card"], aircraft_models=["320"], lowcosts=True,
        no_airport_change=True, no_night_transfers=True, no_complex_transfers=True,
        no_recheckin_transfers=True, no_interlines=True, convenient_transfers=True,
        changeable_only=True, refundable_only=True, large_handbag=True))
    st = build_filters_state(it)
    assert st["airlines"] == ["SU", "KC"] and st["alliances"] == ["1"]
    assert st["agents"] == ["aviasales|1"] and st["payment_methods"] == ["card"]
    assert st["equipments"] == ["320"] and st["lowcosts"] is True
    assert st["transfers_without_airport_change"] is True
    assert st["without_night_transfers"] is True
    assert st["transfers_without_virtual_interline_baggage"] is True
    assert st["transfers_without_virtual_interline_convenient"] is True
    assert st["without_interlines"] is True and st["convenient_transfers_v2"] is True
    assert st["change_available_filter"] is True and st["return_available_filter"] is True
    assert st["large_handbag"] is True


def test_per_direction_times_and_airports():
    it = make_itinerary(
        per_direction={1: Constraints(depart_time=(dt.time(6, 0), dt.time(12, 0)),
                                      arrive_time=(dt.time(18, 0), dt.time(23, 30)))},
        direction_airports={0: (["DME"], None), 3: (None, ["SVO"])},
        same_airport_cities=["MOW"],
    )
    st = build_filters_state(it)
    assert st["segments.departure_time|1"] == {"min": 360, "max": 720}
    assert st["segments.arrival_time|1"] == {"min": 1080, "max": 1410}
    assert st["airports|MOW"] == ["DME"]          # from_airports направления 0 (город MOW)
    assert st["with_same_departure_arrival_airport|MOW"] is True


def test_time_of_day_maps_to_segment_ranges():
    it = make_itinerary(per_direction={0: Constraints(depart_time_of_day=[TimeOfDay.MORNING])})
    assert build_filters_state(it)["segments.departure_time|0"] == {"min": 360, "max": 720}


# --------------------- позиционная семантика *_time_of_day ---------------------
# depart_time_of_day/arrive_time_of_day из global_constraints исторически
# позиционные: применяются только к направлению 0 (depart) / последнему
# (arrive). per-direction значение применяется всегда на своём направлении,
# в т.ч. на промежуточном.

def test_global_depart_time_of_day_applies_only_to_first_direction():
    it = make_itinerary(
        num_directions=3,
        global_constraints=Constraints(depart_time_of_day=[TimeOfDay.MORNING]),
    )
    st = build_filters_state(it)
    assert st["segments.departure_time|0"] == {"min": 360, "max": 720}
    assert "segments.departure_time|1" not in st
    assert "segments.departure_time|2" not in st


def test_global_arrive_time_of_day_applies_only_to_last_direction():
    it = make_itinerary(
        num_directions=3,
        global_constraints=Constraints(arrive_time_of_day=[TimeOfDay.EVENING]),
    )
    st = build_filters_state(it)
    assert "segments.arrival_time|0" not in st
    assert "segments.arrival_time|1" not in st
    assert st["segments.arrival_time|2"] == {"min": 1080, "max": 1440}


def test_per_direction_time_of_day_applies_on_its_own_middle_direction():
    it = make_itinerary(
        num_directions=3,
        per_direction={1: Constraints(depart_time_of_day=[TimeOfDay.AFTERNOON])},
    )
    st = build_filters_state(it)
    assert "segments.departure_time|0" not in st
    assert st["segments.departure_time|1"] == {"min": 720, "max": 1080}
    assert "segments.departure_time|2" not in st


# --------------------- финальное ревью: airports|CITY на кольцевом маршруте ---------------------


def test_segment_state_merges_airports_on_repeated_city_ring_route():
    """Кольцевой маршрут MOW->IST->MOW: from_airports направления 0 (аэропорт
    вылета из MOW) и to_airports направления 1 (аэропорт прилёта в MOW) метят
    один и тот же ключ airports|MOW -> оба аэропорта должны попасть в
    объединённый список, а не затирать друг друга (иначе сервер+клиент вместе
    дают молчаливый 0 результатов)."""
    config = {
        "directions": [
            {
                "from": "MOW", "to": "IST",
                "date_window": {"earliest": "2026-09-01", "latest": "2026-09-01"},
                "from_airports": ["DME"],
            },
            {
                "from": "IST", "to": "MOW",
                "date_window": {"earliest": "2026-09-08", "latest": "2026-09-08"},
                "to_airports": ["SVO"],
            },
        ],
    }
    itinerary = parse_config(config)
    st = build_filters_state(itinerary)
    assert set(st["airports|MOW"]) == {"DME", "SVO"}
