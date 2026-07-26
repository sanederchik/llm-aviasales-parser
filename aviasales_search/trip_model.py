from __future__ import annotations

import datetime as dt
import enum
from dataclasses import dataclass
from typing import Optional


class ConfigError(ValueError):
    """Ошибка валидации пользовательского конфига поездки."""


class TimeOfDay(enum.Enum):
    MORNING = (6, 12)
    AFTERNOON = (12, 18)
    EVENING = (18, 24)
    NIGHT = (0, 6)

    def contains(self, t: dt.time) -> bool:
        start, end = self.value
        return start <= t.hour < end

    @classmethod
    def parse(cls, s: str) -> "TimeOfDay":
        try:
            return cls[s.strip().upper()]
        except KeyError as exc:
            raise ConfigError(
                f"неизвестное время суток: {s!r} "
                f"(допустимо: morning/afternoon/evening/night)"
            ) from exc


@dataclass
class Passengers:
    adults: int = 1
    children: int = 0
    infants: int = 0

    def total(self) -> int:
        return self.adults + self.children + self.infants


@dataclass
class DateWindow:
    earliest: dt.date
    latest: dt.date

    def days(self) -> list[dt.date]:
        n = (self.latest - self.earliest).days
        return [self.earliest + dt.timedelta(days=i) for i in range(n + 1)]


# Персидский залив — дефолтный список аэропортов пересадок, исключаемых при
# `exclude_gulf_transfers: true` (см. Global Constraints рефактора v3.2).
GULF_AIRPORTS: frozenset[str] = frozenset({
    "DXB", "DWC", "AUH", "SHJ", "RKT", "DOH", "BAH", "KWI", "RUH", "JED", "DMM", "MCT",
})


@dataclass
class Constraints:
    max_transfers: Optional[int] = None
    max_transfer_minutes: Optional[int] = None
    baggage_required: Optional[bool] = None
    depart_time_of_day: Optional[list[TimeOfDay]] = None
    arrive_time_of_day: Optional[list[TimeOfDay]] = None
    max_duration_minutes: Optional[int] = None
    airlines: Optional[list[str]] = None
    exclude_transfer_airports: Optional[set[str]] = None

    def merged_over(self, base: "Constraints") -> "Constraints":
        out = Constraints()
        for f in Constraints.__dataclass_fields__:
            mine = getattr(self, f)
            setattr(out, f, mine if mine is not None else getattr(base, f))
        return out


@dataclass
class Direction:
    origin: str
    destination: str
    date_window: DateWindow


@dataclass
class SearchBudget:
    max_requests: int = 100
    date_samples_per_direction: int = 10


@dataclass
class CacheCfg:
    ttl_minutes: int = 5


@dataclass
class Itinerary:
    currency: str
    market_code: str
    trip_class: str
    passengers: Passengers
    directions: list[Direction]
    global_constraints: Constraints
    per_direction_constraints: list[Constraints]
    search_budget: SearchBudget
    cache: CacheCfg
    # Макс. число дней между датой вылета ПЕРВОГО направления и датой вылета
    # ПОСЛЕДНЕГО (длительность всей поездки); None — без ограничения.
    max_trip_days: Optional[int] = None

    def effective_constraints(self, i: int) -> Constraints:
        return self.per_direction_constraints[i].merged_over(self.global_constraints)


# --------------------------- результаты поиска ---------------------------


@dataclass
class FlightLeg:
    origin: str
    destination: str
    departure: dt.datetime
    arrival: dt.datetime
    carrier: str
    flight_number: str
    # Unix seconds (UTC-unambiguous), used for cross-timezone-correct duration/gap
    # math. `departure`/`arrival` above stay naive LOCAL datetimes on purpose -
    # they're correct for time-of-day filters and report display.
    departure_ts: int = 0
    arrival_ts: int = 0
    # Human-readable airline name (e.g. "China Southern Airlines"), resolved from
    # the poll chunk's `airlines` map; falls back to the IATA `carrier` code when
    # unknown/unavailable.
    carrier_name: str = ""


@dataclass
class DirectionResult:
    legs: list[FlightLeg]

    @property
    def transfers(self) -> int:
        return len(self.legs) - 1

    @property
    def duration_minutes(self) -> int:
        """Timezone-correct: derived from unix timestamps, not the naive local
        departure/arrival datetimes (which would be inflated/deflated by the
        UTC-offset delta between origin and destination timezones)."""
        return (self.legs[-1].arrival_ts - self.legs[0].departure_ts) // 60

    @property
    def transfer_airports(self) -> list[str]:
        return [leg.destination for leg in self.legs[:-1]]

    @property
    def main_carrier(self) -> str:
        return self.legs[0].carrier

    @property
    def main_carrier_name(self) -> str:
        return self.legs[0].carrier_name or self.legs[0].carrier

    @property
    def depart(self) -> dt.datetime:
        return self.legs[0].departure

    @property
    def arrive(self) -> dt.datetime:
        return self.legs[-1].arrival


@dataclass
class Ticket:
    price_rub: int
    directions: list[DirectionResult]
    has_baggage: bool
    deep_link: str
    signature: str = ""

    @property
    def route(self) -> str:
        parts = [f"{d.legs[0].origin}→{d.legs[-1].destination}" for d in self.directions]
        return " ⇄ ".join(parts)


# --------------------------- parsing ---------------------------

def _require(data: dict, key: str, ctx: str):
    if key not in data:
        raise ConfigError(f"{ctx}: обязательное поле {key!r} отсутствует")
    return data[key]


def _parse_date(s: str, ctx: str) -> dt.date:
    try:
        return dt.date.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"{ctx}: дата {s!r} не в формате YYYY-MM-DD") from exc


def _parse_exclude_transfer_airports(data: dict) -> Optional[set[str]]:
    if "exclude_transfer_airports" in data:
        return set(data["exclude_transfer_airports"])
    if data.get("exclude_gulf_transfers"):
        return set(GULF_AIRPORTS)
    return None


def _parse_constraints(data: Optional[dict]) -> Constraints:
    if not data:
        return Constraints()
    tod_depart = data.get("depart_time_of_day")
    tod_arrive = data.get("arrive_time_of_day")
    return Constraints(
        max_transfers=data.get("max_transfers"),
        max_transfer_minutes=data.get("max_transfer_minutes"),
        baggage_required=data.get("baggage_required"),
        depart_time_of_day=[TimeOfDay.parse(x) for x in tod_depart] if tod_depart else None,
        arrive_time_of_day=[TimeOfDay.parse(x) for x in tod_arrive] if tod_arrive else None,
        max_duration_minutes=data.get("max_duration_minutes"),
        airlines=data.get("airlines"),
        exclude_transfer_airports=_parse_exclude_transfer_airports(data),
    )


def _parse_date_window(data: dict, ctx: str) -> DateWindow:
    raw = data.get("date_window")
    if raw is None:
        raise ConfigError(
            f"{ctx}: обязательное поле 'date_window' отсутствует "
            f"(у каждого направления должно быть окно дат)"
        )
    dw = DateWindow(
        earliest=_parse_date(_require(raw, "earliest", ctx), ctx),
        latest=_parse_date(_require(raw, "latest", ctx), ctx),
    )
    if dw.latest < dw.earliest:
        raise ConfigError(f"{ctx}: latest < earliest в date_window")
    return dw


def _parse_direction(data: dict, ctx: str) -> tuple[Direction, Constraints]:
    direction = Direction(
        origin=str(_require(data, "from", ctx)),
        destination=str(_require(data, "to", ctx)),
        date_window=_parse_date_window(data, ctx),
    )
    return direction, _parse_constraints(data.get("constraints"))


def _parse_cache(cache: dict) -> CacheCfg:
    """`ttl_minutes` — основное поле; легаси `ttl_hours` (старые конфиги)
    конвертируется в минуты, `ttl_minutes` при обоих заданных выигрывает."""
    if "ttl_minutes" in cache:
        return CacheCfg(ttl_minutes=cache["ttl_minutes"])
    if "ttl_hours" in cache:
        return CacheCfg(ttl_minutes=cache["ttl_hours"] * 60)
    return CacheCfg()


def _parse_max_trip_days(data: dict) -> Optional[int]:
    value = data.get("max_trip_days")
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(
            f"config: 'max_trip_days' должен быть положительным целым, получено {value!r}"
        )
    return value


def parse_config(data: dict) -> Itinerary:
    directions_raw = _require(data, "directions", "config")
    if not isinstance(directions_raw, list) or not directions_raw:
        raise ConfigError("config: 'directions' должен быть непустым списком")
    pax = data.get("passengers", {})
    budget = data.get("search_budget", {})
    cache = data.get("cache", {})

    parsed = [
        _parse_direction(d, f"direction {i}") for i, d in enumerate(directions_raw)
    ]
    directions = [direction for direction, _ in parsed]
    per_direction_constraints = [constraints for _, constraints in parsed]

    return Itinerary(
        currency=data.get("currency", "rub"),
        market_code=data.get("market_code", "ru"),
        trip_class=data.get("trip_class", "Y"),
        passengers=Passengers(
            adults=pax.get("adults", 1),
            children=pax.get("children", 0),
            infants=pax.get("infants", 0),
        ),
        directions=directions,
        global_constraints=_parse_constraints(data.get("global_constraints")),
        per_direction_constraints=per_direction_constraints,
        search_budget=SearchBudget(
            max_requests=budget.get("max_requests", SearchBudget.max_requests),
            date_samples_per_direction=budget.get(
                "date_samples_per_direction", SearchBudget.date_samples_per_direction,
            ),
        ),
        cache=_parse_cache(cache),
        max_trip_days=_parse_max_trip_days(data),
    )
