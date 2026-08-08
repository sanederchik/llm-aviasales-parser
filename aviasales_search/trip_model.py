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


@dataclass
class StayDays:
    """Пребывание в пункте назначения направления: дата вылета СЛЕДУЮЩЕГО
    направления должна попасть в [дата_вылета + min_days, дата_вылета +
    max_days] (обе границы включительно). Считается по датам вылета —
    погрешность ±1 день от времени прилёта принята сознательно (фильтр
    работает до сетевых запросов, см. спеку 2026-07-28)."""

    min_days: Optional[int] = None
    max_days: Optional[int] = None


# Персидский залив — дефолтный список аэропортов пересадок, исключаемых при
# `exclude_gulf_transfers: true` (см. Global Constraints рефактора v3.2).
GULF_AIRPORTS: frozenset[str] = frozenset({
    "DXB", "DWC", "AUH", "SHJ", "RKT", "DOH", "BAH", "KWI", "RUH", "JED", "DMM", "MCT",
})


@dataclass
class Constraints:
    max_transfers: Optional[int] = None
    min_transfers: Optional[int] = None
    max_transfer_minutes: Optional[int] = None
    baggage_required: Optional[bool] = None
    baggage_min_weight_kg: Optional[int] = None
    large_handbag: Optional[bool] = None
    depart_time_of_day: Optional[list[TimeOfDay]] = None
    arrive_time_of_day: Optional[list[TimeOfDay]] = None
    max_duration_minutes: Optional[int] = None
    airlines: Optional[list[str]] = None
    exclude_transfer_airports: Optional[set[str]] = None
    max_price: Optional[int] = None
    alliances: Optional[list[str]] = None
    agents: Optional[list[str]] = None
    payment_methods: Optional[list[str]] = None
    aircraft_models: Optional[list[str]] = None
    lowcosts: Optional[bool] = None
    no_airport_change: Optional[bool] = None
    no_night_transfers: Optional[bool] = None
    no_complex_transfers: Optional[bool] = None
    no_recheckin_transfers: Optional[bool] = None
    no_interlines: Optional[bool] = None
    convenient_transfers: Optional[bool] = None
    changeable_only: Optional[bool] = None
    refundable_only: Optional[bool] = None
    depart_time: Optional[tuple[dt.time, dt.time]] = None
    arrive_time: Optional[tuple[dt.time, dt.time]] = None

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
    stay_days: Optional[StayDays] = None
    from_airports: Optional[list[str]] = None
    to_airports: Optional[list[str]] = None


@dataclass
class SearchBudget:
    max_requests: int = 100
    date_samples_per_direction: int = 10
    # Пауза перед каждым HTTP-вызовом, секунды (диапазон «человеческих» пауз);
    # настраивается в конфиге: search_budget.request_delay_seconds: {min, max}.
    delay_min_seconds: float = 0.5
    delay_max_seconds: float = 1.0


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
    same_airport_cities: Optional[list[str]] = None

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
    # Минимальный вес разрешённого багажа (кг) выбранного предложения —
    # "оптимистичный": считается по плечам, ГДЕ БАГАЖ ЕСТЬ, плечи без багажа
    # игнорируются, поэтому вес показан и когда багаж есть не на всех плечах;
    # None — вес неизвестен (нет данных) или багажа в предложении нет вовсе.
    baggage_weight_kg: Optional[int] = None
    # ID агента (продавца) выбранного предложения; None, если неизвестен
    # (напр. старая запись кэша до появления поля).
    agent_id: Optional[int] = None
    # additional_tariff_info.change_before_flight/return_before_flight.available
    # выбранного предложения, агрегированные по всем плечам (True только если
    # доступно на КАЖДОМ плече); None — данные неизвестны (нет
    # additional_tariff_info хотя бы на одном плече).
    changeable: Optional[bool] = None
    refundable: Optional[bool] = None

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


def _parse_baggage(data: dict) -> tuple[Optional[bool], Optional[int], Optional[bool]]:
    """Новый формат: "baggage": {"required": bool, "min_weight_kg": int,
    "large_handbag": bool}; легаси "baggage_required": bool. Вес подразумевает
    required=True; large_handbag — независимый флаг ручной клади."""
    raw = data.get("baggage")
    if raw is None:
        return data.get("baggage_required"), None, None
    if not isinstance(raw, dict):
        raise ConfigError(f"constraints.baggage: ожидается объект, получено {raw!r}")
    weight = raw.get("min_weight_kg")
    if weight is not None and (
        isinstance(weight, bool) or not isinstance(weight, int) or weight <= 0
    ):
        raise ConfigError(
            f"constraints.baggage.min_weight_kg: положительное целое, получено {weight!r}")
    required = raw.get("required")
    if weight is not None:
        required = True
    return required, weight, raw.get("large_handbag")


def _parse_str_list(data: dict, key: str) -> Optional[list[str]]:
    """Парсит список строк из конфига, вызывает ConfigError если не список."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ConfigError(f"constraints.{key}: ожидается список, получено {value!r}")
    return value


def _parse_bool(data: dict, key: str) -> Optional[bool]:
    """Парсит булево значение из конфига, вызывает ConfigError если не bool."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ConfigError(f"constraints.{key}: ожидается bool, получено {value!r}")
    return value


def _parse_time_range(time_dict: dict, ctx: str) -> tuple[dt.time, dt.time]:
    """Парсит {"from": "HH:MM", "to": "HH:MM"} в пару dt.time."""
    try:
        from_time = dt.time.fromisoformat(_require(time_dict, "from", ctx))
        to_time = dt.time.fromisoformat(_require(time_dict, "to", ctx))
        return (from_time, to_time)
    except (ValueError, TypeError) as exc:
        raise ConfigError(
            f"{ctx}: время должно быть в формате HH:MM (ISO 8601)"
        ) from exc


def _parse_constraints(data: Optional[dict]) -> Constraints:
    if not data:
        return Constraints()

    # Парсим багаж
    baggage_required, baggage_min_weight_kg, large_handbag = _parse_baggage(data)

    # Парсим max_price (должен быть положительным)
    max_price = data.get("max_price")
    if max_price is not None and (not isinstance(max_price, int) or isinstance(max_price, bool) or max_price <= 0):
        raise ConfigError(f"constraints.max_price: положительное целое, получено {max_price!r}")

    # Парсим времена вылета/прилёта
    depart_time = None
    if "depart_time" in data:
        depart_time = _parse_time_range(data["depart_time"], "constraints.depart_time")

    arrive_time = None
    if "arrive_time" in data:
        arrive_time = _parse_time_range(data["arrive_time"], "constraints.arrive_time")

    # Парсим время суток (legacy)
    tod_depart = data.get("depart_time_of_day")
    tod_arrive = data.get("arrive_time_of_day")

    return Constraints(
        max_transfers=data.get("max_transfers"),
        min_transfers=data.get("min_transfers"),
        max_transfer_minutes=data.get("max_transfer_minutes"),
        baggage_required=baggage_required,
        baggage_min_weight_kg=baggage_min_weight_kg,
        large_handbag=large_handbag,
        depart_time_of_day=[TimeOfDay.parse(x) for x in tod_depart] if tod_depart else None,
        arrive_time_of_day=[TimeOfDay.parse(x) for x in tod_arrive] if tod_arrive else None,
        max_duration_minutes=data.get("max_duration_minutes"),
        airlines=data.get("airlines"),
        exclude_transfer_airports=_parse_exclude_transfer_airports(data),
        max_price=max_price,
        alliances=_parse_str_list(data, "alliances"),
        agents=_parse_str_list(data, "agents"),
        payment_methods=_parse_str_list(data, "payment_methods"),
        aircraft_models=_parse_str_list(data, "aircraft_models"),
        lowcosts=_parse_bool(data, "lowcosts"),
        no_airport_change=_parse_bool(data, "no_airport_change"),
        no_night_transfers=_parse_bool(data, "no_night_transfers"),
        no_complex_transfers=_parse_bool(data, "no_complex_transfers"),
        no_recheckin_transfers=_parse_bool(data, "no_recheckin_transfers"),
        no_interlines=_parse_bool(data, "no_interlines"),
        convenient_transfers=_parse_bool(data, "convenient_transfers"),
        changeable_only=_parse_bool(data, "changeable_only"),
        refundable_only=_parse_bool(data, "refundable_only"),
        depart_time=depart_time,
        arrive_time=arrive_time,
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


def _parse_stay_days(data: dict, ctx: str) -> Optional[StayDays]:
    raw = data.get("stay_days")
    if raw is None:
        return None

    def bound(key: str) -> Optional[int]:
        value = raw.get(key)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ConfigError(
                f"{ctx}: stay_days.{key} должен быть неотрицательным целым, "
                f"получено {value!r}"
            )
        return value

    min_days, max_days = bound("min"), bound("max")
    if min_days is None and max_days is None:
        raise ConfigError(f"{ctx}: stay_days должен содержать 'min' и/или 'max'")
    if min_days is not None and max_days is not None and min_days > max_days:
        raise ConfigError(f"{ctx}: stay_days.min > stay_days.max")
    return StayDays(min_days=min_days, max_days=max_days)


def _parse_direction(data: dict, ctx: str) -> tuple[Direction, Constraints]:
    direction = Direction(
        origin=str(_require(data, "from", ctx)),
        destination=str(_require(data, "to", ctx)),
        date_window=_parse_date_window(data, ctx),
        stay_days=_parse_stay_days(data, ctx),
        from_airports=_parse_str_list(data, "from_airports"),
        to_airports=_parse_str_list(data, "to_airports"),
    )
    return direction, _parse_constraints(data.get("constraints"))


def _parse_delay_bound(delay: dict, key: str, default: float) -> float:
    value = delay.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"search_budget.request_delay_seconds.{key}: ожидается число, "
            f"получено {value!r}")
    if value < 0:
        raise ConfigError(
            f"search_budget.request_delay_seconds.{key}: пауза не может быть "
            f"отрицательной ({value})")
    return float(value)


def _parse_search_budget(budget: dict) -> SearchBudget:
    delay = budget.get("request_delay_seconds", {})
    delay_min = _parse_delay_bound(delay, "min", SearchBudget.delay_min_seconds)
    delay_max = _parse_delay_bound(delay, "max", SearchBudget.delay_max_seconds)
    if delay_min > delay_max:
        raise ConfigError(
            f"search_budget.request_delay_seconds: min ({delay_min}) больше "
            f"max ({delay_max})")
    return SearchBudget(
        max_requests=budget.get("max_requests", SearchBudget.max_requests),
        date_samples_per_direction=budget.get(
            "date_samples_per_direction", SearchBudget.date_samples_per_direction,
        ),
        delay_min_seconds=delay_min,
        delay_max_seconds=delay_max,
    )


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

    if directions[-1].stay_days is not None:
        raise ConfigError(
            "config: 'stay_days' на последнем направлении не имеет смысла "
            "(после него нет следующего вылета)"
        )

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
        search_budget=_parse_search_budget(budget),
        cache=_parse_cache(cache),
        max_trip_days=_parse_max_trip_days(data),
        same_airport_cities=_parse_str_list(data, "same_airport_cities"),
    )
