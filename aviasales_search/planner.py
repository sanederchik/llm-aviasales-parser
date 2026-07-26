"""Планнер по сетке дат: для каждой комбинации дат окон направлений вызывает
`SearchClient.search` (весь маршрут одним запросом), кэширует сырой результат по
`probe_key`, фильтрует полученные билеты через `passes_itinerary` и агрегирует все
прошедшие билеты со всех комбинаций в один список, отсортированный по цене.

Сетевой бюджет (`search_budget.max_requests`) ограничивает только РЕАЛЬНЫЕ сетевые
поиски — кэш-хиты бесплатны. Комбинации дат сами по себе уже являются выборкой
(`sample_dates` по `date_samples_per_direction`) с фильтром монотонности дат между
направлениями, но даже так их может быть больше, чем сетевой бюджет — в этом случае
лишние (сверх бюджета) сетевые пробы просто пропускаются (`continue`), что и
логируется.
"""

from __future__ import annotations

import datetime as dt
import itertools
import logging
from dataclasses import dataclass
from typing import Optional, Protocol

from .cache import ProbeCache, probe_key
from .filters import passes_itinerary
from .trip_model import DirectionResult, FlightLeg, Itinerary, Passengers, Ticket

logger = logging.getLogger(__name__)


class _Client(Protocol):
    def search(
        self,
        dated_directions,
        passengers,
        trip_class: str,
        market_code: str,
        currency_code: str,
        baggage_required: bool = False,
    ) -> list[Ticket]: ...


def sample_dates(days: list[dt.date], n: int) -> list[dt.date]:
    """Равномерная выборка `n` дат из `days`, всегда включающая первый и последний
    день (если `n >= 2`)."""
    if n <= 0 or not days:
        return []
    if len(days) <= n:
        return list(days)
    if n == 1:
        return [days[0]]
    step = (len(days) - 1) / (n - 1)
    idx = sorted({round(i * step) for i in range(n)})
    return [days[i] for i in idx]


def date_combinations(itinerary: Itinerary, samples: int) -> list[tuple[dt.date, ...]]:
    """Декартово произведение выборок дат по каждому направлению (по `samples` дат
    на направление), отфильтрованное по монотонности: дата направления i+1 должна
    быть >= даты направления i (маршрут упорядочен во времени). При заданном
    `max_trip_days` дополнительно отбрасываются комбинации, где между датами
    первого и последнего направлений больше `max_trip_days` дней."""
    per_direction_dates = [
        sample_dates(direction.date_window.days(), samples) for direction in itinerary.directions
    ]
    combos = []
    for combo in itertools.product(*per_direction_dates):
        if not all(combo[i] <= combo[i + 1] for i in range(len(combo) - 1)):
            continue
        if (
            itinerary.max_trip_days is not None
            and (combo[-1] - combo[0]).days > itinerary.max_trip_days
        ):
            continue
        combos.append(combo)
    return combos


def search_page_url(dated_directions, passengers: Passengers) -> str:
    """Ссылка на страницу поиска aviasales с точными датами варианта
    (`https://www.aviasales.ru/search/MOW1509DPS15122`). Per-ticket deep link
    API не отдаёт (см. docs/aviasales-api-v3.2.md); поддерживаются только one-way
    и туда-обратно — для прочих форм возвращается "" (ссылка не рендерится).
    """
    def ddmm(date_iso: str) -> str:
        _, month, day = date_iso.split("-")
        return f"{day}{month}"

    pax = f"{passengers.adults}{passengers.children}{passengers.infants}"
    while len(pax) > 1 and pax.endswith("0"):
        pax = pax[:-1]

    dirs = list(dated_directions)
    if len(dirs) == 1:
        (origin, destination, date_iso) = dirs[0]
        route = f"{origin}{ddmm(date_iso)}{destination}"
    elif (
        len(dirs) == 2
        and dirs[1][0] == dirs[0][1]  # обратное направление, не open-jaw
        and dirs[1][1] == dirs[0][0]
    ):
        (origin, destination, out_iso) = dirs[0]
        route = f"{origin}{ddmm(out_iso)}{destination}{ddmm(dirs[1][2])}"
    else:
        return ""
    return f"https://www.aviasales.ru/search/{route}{pax}"


def _local_as_utc_ts(d: dt.datetime) -> int:
    """Timestamp формата параметра `t`: локальное время рейса, взятое КАК UTC
    (aviasales так кодирует времена в share-ссылке — это НЕ настоящий unix-ts
    рейса). Проверено вживую сопоставлением share-ссылки с ответом API."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def build_ticket_share_url(dated_directions, passengers: Passengers, ticket: "Ticket") -> str:
    """Share-ссылка на КОНКРЕТНЫЙ билет (`…/search/<path>?t=<t>`), которая при
    открытии авто-раскрывает карточку этого билета. Формат `t` вскрыт вживую
    (browser + сверка с API):
      <перевозчик> {dep_ts}{arr_ts}000000{аэропорты} …на направление… _<signature>_<цена>
    где dep/arr — локальное время как UTC (см. `_local_as_utc_ts`), аэропорты —
    origin первого лега + destination каждого лега. 6 средних цифр — неидентифи-
    цирующий флаг (сервер матчит по signature+аэропортам), эмитим 000000.

    Без `signature` (напр. запись из старого кэша) или для неподдерживаемой формы
    маршрута падаем на ссылку-поиск с датами (`search_page_url`)."""
    base = search_page_url(dated_directions, passengers)
    if not base or not ticket.signature:
        return base
    carrier = ticket.directions[0].legs[0].carrier if ticket.directions else ""
    segs = []
    for direction in ticket.directions:
        legs = direction.legs
        airports = legs[0].origin + "".join(leg.destination for leg in legs)
        segs.append(
            f"{_local_as_utc_ts(legs[0].departure)}"
            f"{_local_as_utc_ts(legs[-1].arrival)}000000{airports}"
        )
    t = f"{carrier}{''.join(segs)}_{ticket.signature}_{ticket.price_rub}"
    return f"{base}?t={t}"


@dataclass
class Itin:
    """Результат одной комбинации дат: билеты, УЖЕ прошедшие `passes_itinerary`."""

    dated_dirs: tuple
    tickets: list[Ticket]

    @property
    def best_ticket(self) -> Optional[Ticket]:
        if not self.tickets:
            return None
        return min(self.tickets, key=lambda t: t.price_rub)


def _ticket_to_dict(t: Ticket) -> dict:
    return {
        "price_rub": t.price_rub,
        "has_baggage": t.has_baggage,
        "deep_link": t.deep_link,
        "signature": t.signature,
        "directions": [
            {
                "legs": [
                    {
                        "origin": leg.origin,
                        "destination": leg.destination,
                        "departure": leg.departure.isoformat(),
                        "arrival": leg.arrival.isoformat(),
                        "carrier": leg.carrier,
                        "flight_number": leg.flight_number,
                        "departure_ts": leg.departure_ts,
                        "arrival_ts": leg.arrival_ts,
                        "carrier_name": leg.carrier_name,
                    }
                    for leg in direction.legs
                ],
            }
            for direction in t.directions
        ],
    }


def _ticket_from_dict(d: dict) -> Ticket:
    directions = [
        DirectionResult(
            legs=[
                FlightLeg(
                    origin=leg["origin"],
                    destination=leg["destination"],
                    departure=dt.datetime.fromisoformat(leg["departure"]),
                    arrival=dt.datetime.fromisoformat(leg["arrival"]),
                    carrier=leg["carrier"],
                    flight_number=leg["flight_number"],
                    departure_ts=leg.get("departure_ts", 0),
                    arrival_ts=leg.get("arrival_ts", 0),
                    carrier_name=leg.get("carrier_name", ""),
                )
                for leg in direction["legs"]
            ],
        )
        for direction in d["directions"]
    ]
    return Ticket(
        price_rub=d["price_rub"],
        directions=directions,
        has_baggage=d["has_baggage"],
        deep_link=d["deep_link"],
        signature=d.get("signature", ""),
    )


@dataclass
class Planner:
    config: Itinerary
    client: _Client
    cache: ProbeCache
    now: dt.datetime
    refresh: bool = False

    def _dated_directions(self, combo: tuple[dt.date, ...]) -> list[tuple[str, str, str]]:
        return [
            (direction.origin, direction.destination, date.isoformat())
            for direction, date in zip(self.config.directions, combo)
        ]

    def _key(self, dated_directions: list[tuple[str, str, str]], baggage_required: bool) -> str:
        pax = self.config.passengers
        return probe_key(
            dated_directions, pax.adults, pax.children, pax.infants, self.config.trip_class,
            baggage_required=baggage_required,
        )

    def _cache_lookup(self, key: str) -> Optional[list[Ticket]]:
        """Cache-only lookup. None means 'nothing usable in cache' (miss, stale, or
        refresh forcing a network fetch). A cached empty result ([]) is a HIT and is
        returned as []."""
        if self.refresh:
            return None
        cached = self.cache.get(key, self.config.cache.ttl_minutes, self.now)
        if cached is None:
            return None
        return [_ticket_from_dict(x) for x in cached]

    def _network_search(
        self, dated_directions: list[tuple[str, str, str]], key: str, baggage_required: bool,
    ) -> list[Ticket]:
        tickets = self.client.search(
            dated_directions,
            self.config.passengers,
            self.config.trip_class,
            self.config.market_code,
            self.config.currency,
            baggage_required=baggage_required,
        )
        self.cache.put(key, [_ticket_to_dict(t) for t in tickets], self.now)
        return tickets

    def _baggage_required(self) -> bool:
        """#6: если хоть одно направление требует багаж — требуем багаж на всём
        билете (тариф единый на весь билет, не на направление)."""
        return any(
            self.config.effective_constraints(i).baggage_required is True
            for i in range(len(self.config.directions))
        )

    def plan(self) -> list[Ticket]:
        samples = self.config.search_budget.date_samples_per_direction
        combos = date_combinations(self.config, samples)
        budget = self.config.search_budget.max_requests
        if len(combos) > budget:
            logger.info(
                "date_combinations produced %d combinations, exceeding search "
                "budget of %d max_requests; combinations beyond the network budget "
                "are skipped (cache hits remain free)",
                len(combos), budget,
            )

        baggage_required = self._baggage_required()

        itins: list[Itin] = []
        for combo in combos:
            dated_directions = self._dated_directions(combo)
            key = self._key(dated_directions, baggage_required)
            tickets = self._cache_lookup(key)
            if tickets is None:  # cache miss (or refresh) -> needs network
                if budget <= 0:
                    continue  # no network budget left; keep scanning other combinations
                tickets = self._network_search(dated_directions, key, baggage_required)
                budget -= 1
            passing = [t for t in tickets if passes_itinerary(t, self.config)]
            # Заполняем всегда, включая кэш-хиты: старые записи кэша могли быть
            # сохранены без ссылки.
            for t in passing:
                t.deep_link = build_ticket_share_url(
                    dated_directions, self.config.passengers, t,
                )
            itins.append(Itin(dated_dirs=combo, tickets=passing))

        all_tickets = [ticket for itin in itins for ticket in itin.tickets]
        all_tickets.sort(key=lambda t: t.price_rub)
        return all_tickets
