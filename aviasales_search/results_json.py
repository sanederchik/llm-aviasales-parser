"""JSON-первоисточник результатов поиска: полная сериализация списка `Ticket`
и обратный парсер (round-trip без потерь). Сегменты хранят unix-таймстампы —
длительности в проекте считаются из них (таймзоны!), см. `FlightLeg`.
Производные поля плеча (duration_minutes, transfers, carrier…) дублируются
для удобства чтения JSON руками/скриптами, но парсер восстанавливает билеты
только из сегментов. Спека: docs/superpowers/specs/2026-08-05-*.md."""

from __future__ import annotations

import datetime as dt

from .trip_model import DirectionResult, FlightLeg, Ticket

SCHEMA_VERSION = 1


class ResultsJsonError(ValueError):
    """Файл результатов не читается: не тот формат или чужая версия схемы."""


def _leg_to_dict(leg: FlightLeg) -> dict:
    return {
        "origin": leg.origin,
        "destination": leg.destination,
        "departure": leg.departure.isoformat(),
        "arrival": leg.arrival.isoformat(),
        "departure_ts": leg.departure_ts,
        "arrival_ts": leg.arrival_ts,
        "carrier": leg.carrier,
        "carrier_name": leg.carrier_name,
        "flight_number": leg.flight_number,
    }


def _direction_to_dict(d: DirectionResult) -> dict:
    return {
        "route": f"{d.legs[0].origin}→{d.legs[-1].destination}",
        "origin": d.legs[0].origin,
        "destination": d.legs[-1].destination,
        "depart": d.depart.isoformat(),
        "arrive": d.arrive.isoformat(),
        "duration_minutes": d.duration_minutes,
        "transfers": d.transfers,
        "transfer_airports": d.transfer_airports,
        "carrier": d.main_carrier,
        "carrier_name": d.main_carrier_name,
        "legs": [_leg_to_dict(leg) for leg in d.legs],
    }


def tickets_to_json(tickets: list[Ticket], trip: dict,
                    generated_at: dt.datetime) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "trip": trip,
        "offers": [
            {
                "price_rub": t.price_rub,
                "has_baggage": t.has_baggage,
                "deep_link": t.deep_link,
                "route": t.route,
                "signature": t.signature,
                "baggage_weight_kg": t.baggage_weight_kg,
                "agent_id": t.agent_id,
                "changeable": t.changeable,
                "refundable": t.refundable,
                "directions": [_direction_to_dict(d) for d in t.directions],
            }
            for t in sorted(tickets, key=lambda t: t.price_rub)
        ],
    }


def _leg_from_dict(data: dict) -> FlightLeg:
    return FlightLeg(
        origin=data["origin"],
        destination=data["destination"],
        departure=dt.datetime.fromisoformat(data["departure"]),
        arrival=dt.datetime.fromisoformat(data["arrival"]),
        carrier=data["carrier"],
        flight_number=data["flight_number"],
        departure_ts=data["departure_ts"],
        arrival_ts=data["arrival_ts"],
        carrier_name=data["carrier_name"],
    )


def tickets_from_json(data) -> list[Ticket]:
    if not isinstance(data, dict) or "schema_version" not in data:
        raise ResultsJsonError("не файл результатов поиска: нет schema_version")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ResultsJsonError(
            f"неподдерживаемая версия схемы: {data['schema_version']!r} "
            f"(ожидается {SCHEMA_VERSION})")
    try:
        return [
            Ticket(
                price_rub=o["price_rub"],
                has_baggage=o["has_baggage"],
                deep_link=o["deep_link"],
                signature=o.get("signature", ""),
                # Обратная совместимость: файлы результатов до Task 9 этих
                # полей не содержат -> None (данные неизвестны).
                baggage_weight_kg=o.get("baggage_weight_kg"),
                agent_id=o.get("agent_id"),
                changeable=o.get("changeable"),
                refundable=o.get("refundable"),
                directions=[
                    DirectionResult(legs=[_leg_from_dict(leg) for leg in d["legs"]])
                    for d in o["directions"]
                ],
            )
            for o in data["offers"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ResultsJsonError(f"битый файл результатов: {exc}") from exc
