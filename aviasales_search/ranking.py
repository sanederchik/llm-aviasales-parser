"""Чистые функции ранкинга комбинаций дат по сумме поплечевых минимумов.
Сети нет: работает поверх уже собранных цен (LegPrices). Обобщает бывший
planner.date_combinations — принимает готовые списки дат по направлениям
(из LegPrices), а не sampled-сетку."""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass

from .trip_model import Itinerary


@dataclass(frozen=True)
class RankedCombo:
    combo: tuple[dt.date, ...]
    per_leg_min: tuple[int, ...]
    total: int


def _passes_stay_days(itinerary: Itinerary, combo: tuple[dt.date, ...]) -> bool:
    """Пребывание в пункте назначения направления i (по датам вылета, обе
    границы включительно): combo[i+1]-combo[i] в [min_days, max_days]."""
    for i, direction in enumerate(itinerary.directions[:-1]):
        stay = direction.stay_days
        if stay is None:
            continue
        gap = (combo[i + 1] - combo[i]).days
        if stay.min_days is not None and gap < stay.min_days:
            return False
        if stay.max_days is not None and gap > stay.max_days:
            return False
    return True


def combos_from_date_lists(
    itinerary: Itinerary, per_direction_dates: list[list[dt.date]]
) -> list[tuple[dt.date, ...]]:
    """Декартово произведение переданных дат по направлениям, отфильтрованное по
    монотонности (combo[i] <= combo[i+1]), max_trip_days и stay_days."""
    combos = []
    for combo in itertools.product(*per_direction_dates):
        if not all(combo[i] <= combo[i + 1] for i in range(len(combo) - 1)):
            continue
        if (
            itinerary.max_trip_days is not None
            and (combo[-1] - combo[0]).days > itinerary.max_trip_days
        ):
            continue
        if not _passes_stay_days(itinerary, combo):
            continue
        combos.append(combo)
    return combos


def rank_combos(itinerary: Itinerary, leg_prices) -> list[RankedCombo]:
    """Список комбо, отсортированный по возрастанию суммы поплечевых минимумов.
    Даты берутся только те, где у плеча есть прошедшие билеты (leg_prices).
    Тай-брейк по датам — для детерминизма."""
    n = len(itinerary.directions)
    per_direction_dates = [leg_prices.dates_with_tickets(i) for i in range(n)]
    ranked = []
    for combo in combos_from_date_lists(itinerary, per_direction_dates):
        mins = tuple(leg_prices.min_price(i, combo[i]) for i in range(n))
        ranked.append(RankedCombo(combo=combo, per_leg_min=mins, total=sum(mins)))
    ranked.sort(key=lambda r: (r.total, r.combo))
    return ranked
