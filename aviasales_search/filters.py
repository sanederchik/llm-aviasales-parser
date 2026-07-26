from __future__ import annotations

import dataclasses

from .trip_model import Constraints, DirectionResult, Itinerary, Ticket, TimeOfDay


def _time_ok(t, windows: list[TimeOfDay] | None) -> bool:
    if not windows:
        return True
    return any(w.contains(t) for w in windows)


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


def passes_direction(dr: DirectionResult, c: Constraints) -> bool:
    """Проверка одного направления против набора ограничений."""
    if c.max_transfers is not None and dr.transfers > c.max_transfers:
        return False
    if c.max_transfer_minutes is not None and any(
        g > c.max_transfer_minutes for g in _gap_minutes(dr)
    ):
        return False
    if c.max_duration_minutes is not None and dr.duration_minutes > c.max_duration_minutes:
        return False
    if c.exclude_transfer_airports is not None and set(dr.transfer_airports) & c.exclude_transfer_airports:
        return False
    if not _time_ok(dr.depart.time(), c.depart_time_of_day):
        return False
    if not _time_ok(dr.arrive.time(), c.arrive_time_of_day):
        return False
    return True


def _positional(c: Constraints, is_first: bool, is_last: bool) -> Constraints:
    """depart_time_of_day относится к вылету ПЕРВОГО направления, arrive_time_of_day —
    к прилёту ПОСЛЕДНЕГО; для остальных направлений эти поля не должны учитываться."""
    if not is_first and c.depart_time_of_day is not None:
        c = dataclasses.replace(c, depart_time_of_day=None)
    if not is_last and c.arrive_time_of_day is not None:
        c = dataclasses.replace(c, arrive_time_of_day=None)
    return c


def passes(ticket: Ticket, c: Constraints) -> bool:
    """Все направления билета против одного набора ограничений + проверка багажа."""
    n = len(ticket.directions)
    for i, dr in enumerate(ticket.directions):
        eff = _positional(c, is_first=(i == 0), is_last=(i == n - 1))
        if not passes_direction(dr, eff):
            return False
    if c.baggage_required is True and not ticket.has_baggage:
        return False
    return True


def passes_itinerary(ticket: Ticket, itinerary: Itinerary) -> bool:
    """Полный вентиль для планнера: каждое ticket.directions[i] — против
    itinerary.effective_constraints(i); багаж на уровне билета — по global_constraints."""
    n = len(itinerary.directions)
    if len(ticket.directions) != n:
        return False
    for i, dr in enumerate(ticket.directions):
        eff = _positional(
            itinerary.effective_constraints(i), is_first=(i == 0), is_last=(i == n - 1)
        )
        if not passes_direction(dr, eff):
            return False
    if itinerary.global_constraints.baggage_required is True and not ticket.has_baggage:
        return False
    return True


def apply_filters(tickets: list[Ticket], c: Constraints) -> list[Ticket]:
    return [t for t in tickets if passes(t, c)]
