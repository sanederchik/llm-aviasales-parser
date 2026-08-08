"""Itinerary -> filters_state внутреннего API v3.2 (спека 2026-08-07,
таблица фильтров). Единственное место, знающее ключи API: смена схемы
правится только здесь. Неизвестные серверу ключи он молча игнорирует,
клиентские проверки в filters.py остаются страховкой."""
from __future__ import annotations

import datetime as dt

from .trip_model import Constraints, GULF_AIRPORTS, Itinerary, TimeOfDay

# baggage_weight — single_choice выдачи: только эти пороги, ближайший вниз.
_BAGGAGE_WEIGHT_CHOICES = (30, 20, 10)

# Плоские соответствия: поле Constraints -> ключ filters_state.
_BOOL_KEYS = {
    "lowcosts": "lowcosts",
    "large_handbag": "large_handbag",
    "no_airport_change": "transfers_without_airport_change",
    "no_night_transfers": "without_night_transfers",
    "no_complex_transfers": "transfers_without_virtual_interline_baggage",
    "no_recheckin_transfers": "transfers_without_virtual_interline_convenient",
    "no_interlines": "without_interlines",
    "convenient_transfers": "convenient_transfers_v2",
    "changeable_only": "change_available_filter",
    "refundable_only": "return_available_filter",
}
_SET_KEYS = {
    "airlines": "airlines",
    "alliances": "alliances",
    "agents": "agents",
    "payment_methods": "payment_methods",
    "aircraft_models": "equipments",
}


def _minutes(t: dt.time) -> int:
    return t.hour * 60 + t.minute


def _time_range(pair) -> dict:
    return {"min": _minutes(pair[0]), "max": _minutes(pair[1])}


def _time_of_day_range(windows: list[TimeOfDay]) -> dict:
    starts = [w.value[0] for w in windows]
    ends = [w.value[1] for w in windows]
    return {"min": min(starts) * 60, "max": max(ends) * 60}


def _global_state(c: Constraints) -> dict:
    state: dict = {}
    if c.baggage_required is True:
        state["baggage"] = True
    if c.baggage_min_weight_kg is not None:
        state["baggage"] = True
        for choice in _BAGGAGE_WEIGHT_CHOICES:
            if c.baggage_min_weight_kg >= choice:
                state["baggage_weight"] = str(choice)
                break
    if c.max_transfers is not None:
        lo = c.min_transfers if c.min_transfers is not None else 0
        state["transfers_count"] = [str(i) for i in range(lo, c.max_transfers + 1)]
    if c.exclude_transfer_airports is not None and c.exclude_transfer_airports >= GULF_AIRPORTS:
        state["transfers_without_persian_gulf"] = True
    if c.max_duration_minutes is not None:
        state["trip_duration"] = {"min": 0, "max": c.max_duration_minutes}
    if c.max_transfer_minutes is not None:
        state["transfers_duration"] = {"min": 0, "max": c.max_transfer_minutes}
    if c.max_price is not None:
        state["price"] = {"min": 0, "max": c.max_price}
    for field, key in _BOOL_KEYS.items():
        if getattr(c, field) is True:
            state[key] = True
    for field, key in _SET_KEYS.items():
        value = getattr(c, field)
        if value is not None:
            state[key] = list(value)
    return state


def _merge_airports(state: dict, key: str, airports: list[str]) -> None:
    """Пишет `airports[key]` = объединение уже накопленных аэропортов (если
    есть) и `airports`, без дубликатов, порядок по первому появлению.
    Нужно для кольцевых маршрутов (MOW -> X -> MOW): from_airports первого
    направления и to_airports последнего метят один и тот же ключ
    `airports|MOW` -- присвоение затёрло бы одно из них, давая серверу и
    клиенту молчаливо разное представление о допустимых аэропортах."""
    existing = state.get(key)
    if existing is None:
        state[key] = list(dict.fromkeys(airports))
        return
    merged = list(existing)
    for airport in airports:
        if airport not in merged:
            merged.append(airport)
    state[key] = merged


def _segment_state(itinerary: Itinerary) -> dict:
    """`depart_time`/`arrive_time` (точные диапазоны) всегда per-direction —
    применяются на своём i без ограничений. `*_time_of_day` исторически
    позиционные, когда заданы глобально: `depart_time_of_day` из
    global_constraints бьёт только по направлению 0, `arrive_time_of_day` —
    только по последнему; при этом per-direction значение (из constraints
    самого направления) применяется всегда на своём i, в т.ч. промежуточном.
    Источник значения определяется по per_direction_constraints[i] — не None
    там означает per-direction, иначе значение унаследовано из global."""
    state: dict = {}
    last = len(itinerary.directions) - 1
    for i, direction in enumerate(itinerary.directions):
        c = itinerary.effective_constraints(i)
        own = itinerary.per_direction_constraints[i]

        if c.depart_time is not None:
            state[f"segments.departure_time|{i}"] = _time_range(c.depart_time)
        elif c.depart_time_of_day and (own.depart_time_of_day is not None or i == 0):
            state[f"segments.departure_time|{i}"] = _time_of_day_range(c.depart_time_of_day)

        if c.arrive_time is not None:
            state[f"segments.arrival_time|{i}"] = _time_range(c.arrive_time)
        elif c.arrive_time_of_day and (own.arrive_time_of_day is not None or i == last):
            state[f"segments.arrival_time|{i}"] = _time_of_day_range(c.arrive_time_of_day)

        if direction.from_airports:
            _merge_airports(state, f"airports|{direction.origin}", direction.from_airports)
        if direction.to_airports:
            _merge_airports(state, f"airports|{direction.destination}", direction.to_airports)
    for city in itinerary.same_airport_cities or []:
        state[f"with_same_departure_arrival_airport|{city}"] = True
    return state


def build_filters_state(itinerary: Itinerary) -> dict:
    """Собирает filters_state для стартового запроса v3.2 из Itinerary:
    глобальные ключи — из global_constraints, per-direction ключи
    `segments.*|i`/`airports|CITY` — из effective_constraints(i) и
    directions[i]. Всегда добавляет `sort: price_asc` (при лимите 200
    билетов на ответ дешёвые приходят первыми — решение пользователя)."""
    state = _global_state(itinerary.global_constraints)
    state.update(_segment_state(itinerary))
    state["sort"] = "price_asc"
    return state
