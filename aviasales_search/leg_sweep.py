"""Фаза 1: поплечевой свип. Каждое направление ищется как one-way по ВСЕМ датам
своего окна (не sampled), билеты фильтруются per-direction. Не ограничивается
search_budget.max_requests — полный свип всегда. Пробы кэшируются тем же
ProbeCache/ключом, что и combo-пробы Фазы 2 (dated_directions в ключе делает
one-way и multi-city пробы разными записями)."""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from .api_filters import build_filters_state
from .cache import ProbeCache, probe_cache_key
from .filters import passes_itinerary
from .planner import ticket_from_dict, ticket_to_dict
from .progress import ComboEvent, ProgressReporter
from .trip_model import Constraints, Itinerary, Ticket


class _Client(Protocol):
    def search(self, dated_directions, passengers, trip_class: str, market_code: str,
               currency_code: str, baggage_required: bool = False,
               min_baggage_weight_kg: Optional[int] = None,
               filters_state: Optional[dict] = None) -> list[Ticket]: ...


@dataclass
class LegDate:
    date: dt.date
    tickets: list[Ticket]  # прошедшие фильтры one-way билеты, по возрастанию цены

    @property
    def min_price(self) -> Optional[int]:
        return self.tickets[0].price_rub if self.tickets else None

    @property
    def best(self) -> Optional[Ticket]:
        return self.tickets[0] if self.tickets else None


@dataclass
class LegPrices:
    # По индексу направления: {дата -> LegDate} только для дат с >=1 билетом.
    per_direction: list[dict[dt.date, LegDate]] = field(default_factory=list)

    def dates_with_tickets(self, i: int) -> list[dt.date]:
        return sorted(self.per_direction[i])

    def min_price(self, i: int, d: dt.date) -> Optional[int]:
        ld = self.per_direction[i].get(d)
        return ld.min_price if ld else None

    def best(self, i: int, d: dt.date) -> Optional[Ticket]:
        ld = self.per_direction[i].get(d)
        return ld.best if ld else None


def leg_itinerary(config: Itinerary, i: int) -> Itinerary:
    """Одно-направленный под-итинерарий для плеча i: его Direction (с
    from/to_airports), effective_constraints(i) как global_constraints, без
    stay_days. Позиционные *_time_of_day, унаследованные из global, снимаются
    для не-крайних плеч — иначе на промежуточное плечо ошибочно наложился бы
    depart/arrive tod (в multi-city это делает filters._positional). Так и
    filters_state, и клиентская проверка (passes_itinerary) на плече ведут себя
    согласованно."""
    c = config.effective_constraints(i)
    own = config.per_direction_constraints[i]
    last = len(config.directions) - 1
    if own.depart_time_of_day is None and i != 0:
        c = dataclasses.replace(c, depart_time_of_day=None)
    if own.arrive_time_of_day is None and i != last:
        c = dataclasses.replace(c, arrive_time_of_day=None)
    direction = dataclasses.replace(config.directions[i], stay_days=None)
    return dataclasses.replace(
        config, directions=[direction], global_constraints=c,
        per_direction_constraints=[Constraints()], max_trip_days=None,
        same_airport_cities=None,
    )


@dataclass
class LegSweeper:
    config: Itinerary
    client: _Client
    cache: ProbeCache
    now: dt.datetime
    refresh: bool = False
    progress: Optional[ProgressReporter] = None
    # Вызывается после КАЖДОЙ пройденной даты с накопленным LegPrices — так
    # промежуточный отчёт «живой»: при обрыве (бан/Ctrl+C) в любой точке свипа
    # в нём остаётся всё, что успели собрать до обрыва.
    live_report: Optional[Callable[["LegPrices"], None]] = None

    def _search_date(self, sub: Itinerary, dated) -> tuple[list[Ticket], str]:
        """Возвращает (билеты, источник) — источник «кэш» или «сеть», чтобы
        прогресс не врал (кэш-хиты в Фазе 1 не должны показываться как «сеть»)."""
        c = sub.global_constraints
        baggage_required = c.baggage_required is True
        min_weight = c.baggage_min_weight_kg
        filters_state = build_filters_state(sub)
        key = probe_cache_key(dated, self.config.passengers, self.config.trip_class,
                              baggage_required, min_weight, filters_state)
        if not self.refresh:
            cached = self.cache.get(key, self.config.cache.ttl_minutes, self.now)
            if cached is not None:
                return [ticket_from_dict(x) for x in cached], "кэш"
        tickets = self.client.search(
            dated, self.config.passengers, self.config.trip_class,
            self.config.market_code, self.config.currency,
            baggage_required=baggage_required, min_baggage_weight_kg=min_weight,
            filters_state=filters_state,
        )
        self.cache.put(key, [ticket_to_dict(t) for t in tickets], self.now)
        return tickets, "сеть"

    def sweep(self) -> LegPrices:
        total = sum(len(d.date_window.days()) for d in self.config.directions)
        if self.progress is not None:
            self.progress.start(total, total, self.config.cache.ttl_minutes)
        # Слоты всех направлений заводим сразу (пустые dict) — тогда накопленный
        # LegPrices всегда имеет по слоту на направление, и rank_combos/рендер
        # промежуточного отчёта работают на частичных данных (ещё не пройденные
        # плечи пусты → в ранкинге просто нет комбо через них).
        leg_prices = LegPrices(per_direction=[{} for _ in self.config.directions])
        index = 0
        for i, direction in enumerate(self.config.directions):
            sub = leg_itinerary(self.config, i)
            by_date = leg_prices.per_direction[i]
            for d in direction.date_window.days():
                index += 1
                dated = [(direction.origin, direction.destination, d.isoformat())]
                tickets, source = self._search_date(sub, dated)
                passing = sorted(
                    (t for t in tickets if passes_itinerary(t, sub)),
                    key=lambda t: t.price_rub,
                )
                if passing:
                    by_date[d] = LegDate(date=d, tickets=passing)
                if self.progress is not None:
                    best = passing[0].price_rub if passing else None
                    self.progress.emit(ComboEvent(
                        index=index, total=total, dated_dirs=dated, source=source,
                        found=len(tickets), passed=len(passing), best_price=best,
                        running_min=None, improved=False,
                    ))
                if self.live_report is not None:
                    self.live_report(leg_prices)
        return leg_prices
