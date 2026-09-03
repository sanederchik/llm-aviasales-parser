"""Фаза 2: точечная multi-city верификация. Идёт по ранкингу сверху вниз и для
первых top_n комбо делает реальный поиск всей связки одним запросом (как
прежний Planner per-combo), фильтрует через passes_itinerary, копит билеты по
цене. Кэш-хиты бесплатны и не тратят сетевой потолок max_network."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Optional, Protocol

from .api_filters import build_filters_state
from .cache import ProbeCache, probe_cache_key
from .filters import _min_baggage_weight_kg, passes_itinerary
from .planner import build_ticket_share_url, ticket_from_dict, ticket_to_dict
from .progress import ComboEvent, ProgressReporter
from .ranking import RankedCombo
from .trip_model import Itinerary, Ticket


class _Client(Protocol):
    def search(self, dated_directions, passengers, trip_class: str, market_code: str,
               currency_code: str, baggage_required: bool = False,
               min_baggage_weight_kg: Optional[int] = None,
               filters_state: Optional[dict] = None) -> list[Ticket]: ...


def baggage_required_for(config: Itinerary) -> bool:
    """Багаж требуется на всём билете, если хоть одно направление его требует
    (тариф единый на весь билет)."""
    return any(
        config.effective_constraints(i).baggage_required is True
        for i in range(len(config.directions))
    )


def min_baggage_weight_for(config: Itinerary) -> Optional[int]:
    """Максимум per-direction порогов веса (тот же, что в filters)."""
    return _min_baggage_weight_kg(config)


@dataclass
class ComboVerifier:
    config: Itinerary
    client: _Client
    cache: ProbeCache
    now: dt.datetime
    refresh: bool = False
    progress: Optional[ProgressReporter] = None
    live_report: Optional[Callable[[list[Ticket]], None]] = None

    def verify(self, ranked: list[RankedCombo], top_n: int,
               max_network: Optional[int] = None) -> list[Ticket]:
        baggage_required = baggage_required_for(self.config)
        min_weight = min_baggage_weight_for(self.config)
        filters_state = build_filters_state(self.config)
        target = ranked[:top_n]
        if self.progress is not None:
            self.progress.start(
                len(target),
                max_network if max_network is not None else len(target),
                self.config.cache.ttl_minutes,
            )
        network_used = 0
        collected: list[Ticket] = []
        running_min: Optional[int] = None
        for index, rc in enumerate(target, start=1):
            dated = [
                (d.origin, d.destination, date.isoformat())
                for d, date in zip(self.config.directions, rc.combo)
            ]
            key = probe_cache_key(dated, self.config.passengers, self.config.trip_class,
                                  baggage_required, min_weight, filters_state)
            tickets = None
            source = "кэш"
            if not self.refresh:
                cached = self.cache.get(key, self.config.cache.ttl_minutes, self.now)
                if cached is not None:
                    tickets = [ticket_from_dict(x) for x in cached]
            if tickets is None:
                if max_network is not None and network_used >= max_network:
                    if self.progress is not None:
                        self.progress.emit(ComboEvent(
                            index=index, total=len(target), dated_dirs=dated,
                            source="пропуск", found=None, passed=None, best_price=None,
                            running_min=running_min, improved=False))
                    continue
                tickets = self.client.search(
                    dated, self.config.passengers, self.config.trip_class,
                    self.config.market_code, self.config.currency,
                    baggage_required=baggage_required, min_baggage_weight_kg=min_weight,
                    filters_state=filters_state)
                self.cache.put(key, [ticket_to_dict(t) for t in tickets], self.now)
                network_used += 1
                source = "сеть"
            passing = [t for t in tickets if passes_itinerary(t, self.config)]
            for t in passing:
                t.deep_link = build_ticket_share_url(dated, self.config.passengers, t)
            best = min((t.price_rub for t in passing), default=None)
            improved = best is not None and (running_min is None or best < running_min)
            if improved:
                running_min = best
            if self.progress is not None:
                self.progress.emit(ComboEvent(
                    index=index, total=len(target), dated_dirs=dated, source=source,
                    found=len(tickets), passed=len(passing), best_price=best,
                    running_min=running_min, improved=improved))
            collected.extend(passing)
            if passing and self.live_report is not None:
                self.live_report(sorted(collected, key=lambda t: t.price_rub))
        collected.sort(key=lambda t: t.price_rub)
        return collected
