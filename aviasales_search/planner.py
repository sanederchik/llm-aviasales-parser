"""Ссылки на поиск/билет aviasales и (де)сериализация билетов в dict для кэша.

Оркестрация поиска переехала в двухфазный алгоритм: Фаза 1 —
`leg_sweep.LegSweeper` (поплечевой one-way свип), ранкинг — `ranking.rank_combos`,
Фаза 2 — `verifier.ComboVerifier` (multi-city верификация топ-N комбо),
оркестратор — `two_phase.TwoPhasePlanner`. Здесь остаются переиспользуемые ими
чистые хелперы: построение share-ссылок и round-trip ticket↔dict для кэша."""

from __future__ import annotations

import datetime as dt

from .trip_model import DirectionResult, FlightLeg, Passengers, Ticket


def _is_true_roundtrip(dirs: list) -> bool:
    """Форма «настоящий туда-обратно»: ровно 2 плеча, второе — зеркало первого
    (`B->A` после `A->B`). Open-jaw (второе плечо начинается там же, где
    закончилось первое, но летит в третий пункт) под это НЕ подпадает.
    Используется и в `search_page_url` (выбор формата составного кода), и в
    `build_ticket_share_url` (можно ли эмитить `?t=`) — чтобы обе функции
    одинаково понимали, что такое «round-trip»."""
    return (
        len(dirs) == 2
        and dirs[1][0] == dirs[0][1]
        and dirs[1][1] == dirs[0][0]
    )


def search_page_url(dated_directions, passengers: Passengers) -> str:
    """Ссылка на страницу поиска aviasales с точными датами варианта
    (`https://www.aviasales.ru/search/MOW1509DPS15122`). Per-ticket deep link
    API не отдаёт (см. docs/aviasales-api-v3.2.md); поддерживаются one-way,
    туда-обратно и мультигород/open-jaw (составной код маршрута, см. ветку
    ниже) — для этих форм ссылка не рендерится, только если распарсить
    направления не удалось.
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
    elif _is_true_roundtrip(dirs):
        (origin, destination, out_iso) = dirs[0]
        route = f"{origin}{ddmm(out_iso)}{destination}{ddmm(dirs[1][2])}"
    else:
        # Мультигород: конкатенация <ORIGIN><ddmm> по каждому плечу + конечный
        # пункт. Формат верифицирован вживую (см. шаг 5 плана); при изменении
        # формата aviasales ссылка деградирует в обычный поиск на сайте.
        route = "".join(f"{o}{ddmm(date_iso)}" for o, _, date_iso in dirs)
        route += dirs[-1][1]
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

    Формат `?t=` верифицирован вживую ТОЛЬКО для one-way (1 плечо) и настоящего
    туда-обратно (2 плеча, второе — зеркало первого, см. `_is_true_roundtrip`).
    Для любой другой формы — мультигород (3+ плеч) или open-jaw (2 плеча, но
    НЕ туда-обратно) — `?t=` не эмитим: возвращаем голую `search_page_url`.
    То же самое, если нет `signature` (напр. запись из старого кэша)."""
    base = search_page_url(dated_directions, passengers)
    dirs = list(dated_directions)
    supported_form = len(dirs) == 1 or _is_true_roundtrip(dirs)
    if not base or not ticket.signature or not supported_form:
        # Форма проверяется по dated_directions (сетка поиска), а не по
        # ticket.directions — консистентно с веткой round-trip в search_page_url.
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


def ticket_to_dict(t: Ticket) -> dict:
    """Сериализация билета в dict для ProbeCache (round-trip без потерь с
    `ticket_from_dict`)."""
    return {
        "price_rub": t.price_rub,
        "has_baggage": t.has_baggage,
        "deep_link": t.deep_link,
        "signature": t.signature,
        "baggage_weight_kg": t.baggage_weight_kg,
        "agent_id": t.agent_id,
        "changeable": t.changeable,
        "refundable": t.refundable,
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


def ticket_from_dict(d: dict) -> Ticket:
    """Обратная к `ticket_to_dict` (round-trip записи кэша)."""
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
        # Обратная совместимость: старые записи кэша до Task 9 этих полей не
        # содержат -> None (данные неизвестны, не «пересчитывать заново»).
        baggage_weight_kg=d.get("baggage_weight_kg"),
        agent_id=d.get("agent_id"),
        changeable=d.get("changeable"),
        refundable=d.get("refundable"),
    )
