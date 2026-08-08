from __future__ import annotations

import dataclasses

from .trip_model import Constraints, DirectionResult, Itinerary, Ticket, TimeOfDay

# Поля Constraints без клиентской проверки-страховки (см. таблицу спеки
# docs/superpowers/specs/2026-08-07-aviasales-filters-design.md): в ответе
# `results` нет данных, по которым это можно перепроверить на клиенте
# (альянсы, способы оплаты, модели ВС, признак лоукостера, «ночность»/
# «сложность»/«интерлайн»-статус пересадки, «удобные пересадки») — эти поля
# уходят только в filters_state серверного запроса (api_filters.py) и
# полагаются целиком на серверную фильтрацию:
# alliances, payment_methods, aircraft_models, lowcosts, no_night_transfers,
# no_complex_transfers, no_recheckin_transfers, no_interlines,
# convenient_transfers.
#
# large_handbag — тоже без клиентской проверки, но по другой причине: по
# спеке (docs/superpowers/specs/2026-08-07-aviasales-filters-design.md,
# строка large_handbag) возможна клиентская проверка через
# flight_terms[*].handbags.count, но Ticket/results_parser её поля не
# извлекают — при необходимости страховки заводить отдельную задачу на
# парсинг handbags.


def _time_ok(t, windows: list[TimeOfDay] | None) -> bool:
    if not windows:
        return True
    return any(w.contains(t) for w in windows)


def _time_range_ok(t, r: tuple | None) -> bool:
    """Точный диапазон времени (constraints.depart_time/arrive_time) — границы
    включительно, в отличие от TimeOfDay-корзин."""
    if r is None:
        return True
    start, end = r
    return start <= t <= end


def _gap_minutes(dr: DirectionResult) -> list[int]:
    """Паузы между соседними legs внутри направления (для max_transfer_minutes).
    Считаем по unix-таймстампам (не по наивным local datetime) - тот же аэропорт
    пересадки означает одну и ту же таймзону, так что число численно не меняется,
    но так строго корректнее на границах перехода на летнее/зимнее время."""
    legs = dr.legs
    return [
        (nxt.departure_ts - prev.arrival_ts) // 60
        for prev, nxt in zip(legs, legs[1:])
    ]


def _airport_change_ok(dr: DirectionResult) -> bool:
    """no_airport_change: на каждой пересадке внутри направления аэропорт
    прилёта предыдущего плеча должен совпадать с аэропортом вылета следующего."""
    legs = dr.legs
    return all(prev.destination == nxt.origin for prev, nxt in zip(legs, legs[1:]))


def passes_direction(dr: DirectionResult, c: Constraints) -> bool:
    """Проверка одного направления против набора ограничений."""
    if c.max_transfers is not None and dr.transfers > c.max_transfers:
        return False
    if c.min_transfers is not None and dr.transfers < c.min_transfers:
        return False
    if c.max_transfer_minutes is not None and any(
        g > c.max_transfer_minutes for g in _gap_minutes(dr)
    ):
        return False
    if c.max_duration_minutes is not None and dr.duration_minutes > c.max_duration_minutes:
        return False
    if c.exclude_transfer_airports is not None and set(dr.transfer_airports) & c.exclude_transfer_airports:
        return False
    if c.airlines is not None and any(leg.carrier not in c.airlines for leg in dr.legs):
        return False
    if not _time_ok(dr.depart.time(), c.depart_time_of_day):
        return False
    if not _time_ok(dr.arrive.time(), c.arrive_time_of_day):
        return False
    if not _time_range_ok(dr.depart.time(), c.depart_time):
        return False
    if not _time_range_ok(dr.arrive.time(), c.arrive_time):
        return False
    if c.no_airport_change is True and not _airport_change_ok(dr):
        return False
    return True


def _positional(c: Constraints, is_first: bool, is_last: bool) -> Constraints:
    """depart_time_of_day относится к вылету ПЕРВОГО направления, arrive_time_of_day —
    к прилёту ПОСЛЕДНЕГО; для остальных направлений эти поля не должны учитываться.
    Точные depart_time/arrive_time сюда не входят - они, в отличие от корзин
    TimeOfDay, всегда проверяются по-направленчески (каждое направление против
    своих effective_constraints)."""
    if not is_first and c.depart_time_of_day is not None:
        c = dataclasses.replace(c, depart_time_of_day=None)
    if not is_last and c.arrive_time_of_day is not None:
        c = dataclasses.replace(c, arrive_time_of_day=None)
    return c


def _agent_matches(agent_id, agents: list[str]) -> bool:
    """agents: список вида "name|id" (формат filters_state); сопоставляем по
    числовой части id, т.к. читаемое имя агента у нас не хранится."""
    if agent_id is None:
        return False
    ids = {a.split("|")[-1] for a in agents}
    return str(agent_id) in ids


def _ticket_level_ok(ticket: Ticket, c: Constraints) -> bool:
    """Проверки уровня билета целиком (не отдельного направления): багаж
    (наличие + минимальный вес), цена, агент-продавец, признаки обмена/
    возврата тарифа. Общий код для `passes` и `passes_itinerary`."""
    if c.baggage_required is True and not ticket.has_baggage:
        return False
    if c.baggage_min_weight_kg is not None and (
        ticket.baggage_weight_kg is None or ticket.baggage_weight_kg < c.baggage_min_weight_kg
    ):
        return False
    if c.max_price is not None and ticket.price_rub > c.max_price:
        return False
    if c.agents is not None and not _agent_matches(ticket.agent_id, c.agents):
        return False
    # None (данные неизвестны) трактуем как «не подходит» при заданном фильтре -
    # консервативно, чтобы не пропустить билет, для которого мы не смогли
    # подтвердить возможность обмена/возврата.
    if c.changeable_only is True and ticket.changeable is not True:
        return False
    if c.refundable_only is True and ticket.refundable is not True:
        return False
    return True


def passes(ticket: Ticket, c: Constraints) -> bool:
    """Все направления билета против одного набора ограничений + проверки уровня билета."""
    n = len(ticket.directions)
    for i, dr in enumerate(ticket.directions):
        eff = _positional(c, is_first=(i == 0), is_last=(i == n - 1))
        if not passes_direction(dr, eff):
            return False
    return _ticket_level_ok(ticket, c)


def _min_baggage_weight_kg(itinerary: Itinerary) -> int | None:
    """Максимум `baggage_min_weight_kg` по effective_constraints всех
    направлений (тариф единый на весь билет -> самый строгий порог должен
    применяться ко всему билету). Та же семантика, что и
    `planner.Planner._min_baggage_weight` -- держать их в согласии, иначе
    сервер (planner) и клиентская страховка (здесь) разойдутся."""
    weights = [
        itinerary.effective_constraints(i).baggage_min_weight_kg
        for i in range(len(itinerary.directions))
    ]
    weights = [w for w in weights if w is not None]
    return max(weights) if weights else None


def passes_itinerary(ticket: Ticket, itinerary: Itinerary) -> bool:
    """Полный вентиль для планнера: каждое ticket.directions[i] — против
    itinerary.effective_constraints(i) (плюс from_airports/to_airports самого
    направления); проверки уровня билета - по global_constraints, за
    исключением веса багажа: он берётся как максимум per-direction порогов
    (см. `_min_baggage_weight_kg`), а не только из global_constraints -- иначе
    порог, заданный ТОЛЬКО на одном направлении, никогда бы не проверялся."""
    n = len(itinerary.directions)
    if len(ticket.directions) != n:
        return False
    for i, dr in enumerate(ticket.directions):
        eff = _positional(
            itinerary.effective_constraints(i), is_first=(i == 0), is_last=(i == n - 1)
        )
        if not passes_direction(dr, eff):
            return False
        direction = itinerary.directions[i]
        if direction.from_airports and dr.legs[0].origin not in direction.from_airports:
            return False
        if direction.to_airports and dr.legs[-1].destination not in direction.to_airports:
            return False
    effective_global = dataclasses.replace(
        itinerary.global_constraints, baggage_min_weight_kg=_min_baggage_weight_kg(itinerary),
    )
    return _ticket_level_ok(ticket, effective_global)


def apply_filters(tickets: list[Ticket], c: Constraints) -> list[Ticket]:
    return [t for t in tickets if passes(t, c)]
