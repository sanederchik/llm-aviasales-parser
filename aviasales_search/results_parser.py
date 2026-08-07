from __future__ import annotations

import datetime as dt
from typing import Optional

from .trip_model import DirectionResult, FlightLeg, Ticket


def _parse_local(s: str) -> dt.datetime:
    # "2026-09-15 21:15"
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M")


def _chunks(resp) -> list[dict]:
    if isinstance(resp, list):
        return [c for c in resp if isinstance(c, dict) and "tickets" in c]
    if isinstance(resp, dict) and "tickets" in resp:
        return [resp]
    return []


def _airline_name(airlines: dict, code: str) -> str:
    """Resolve a human-readable airline name from the poll chunk's `airlines`
    map (`{"CZ": {"name": {"ru": {"default": "China Southern Airlines"}}}}`),
    falling back to the IATA code when missing. Mirrors
    `.sdd/live/analyze_reference.py:airline_name`."""
    a = airlines.get(code) or {}
    n = a.get("name")
    if isinstance(n, dict):
        ru = n.get("ru")
        n = ru.get("default") if isinstance(ru, dict) else ru
    return n or code


def _leg(fl: dict, airlines: dict) -> FlightLeg:
    d = fl["operating_carrier_designator"]
    carrier = d.get("carrier", "")
    return FlightLeg(
        origin=fl["origin"], destination=fl["destination"],
        departure=_parse_local(fl["local_departure_date_time"]),
        arrival=_parse_local(fl["local_arrival_date_time"]),
        carrier=carrier, flight_number=str(d.get("number", "")),
        departure_ts=fl["departure_unix_timestamp"],
        arrival_ts=fl["arrival_unix_timestamp"],
        carrier_name=_airline_name(airlines, carrier),
    )


def _proposal_qualifies(
    p: dict, baggage_required: bool, min_baggage_weight_kg: Optional[int],
) -> bool:
    """Проверяет предложение против ограничений по багажу: без ограничений
    (baggage_required=False и min_baggage_weight_kg=None) — годится любое.
    Иначе на КАЖДОМ плече (flight_terms) требуется хотя бы 1 место багажа;
    если вдобавок задан порог веса — на каждом плече ещё и `baggage.weight`
    >= порога (отсутствие поля weight при заданном пороге дисквалифицирует
    предложение целиком)."""
    if not baggage_required and min_baggage_weight_kg is None:
        return True
    terms = p.get("flight_terms", {})
    if not terms:
        return False
    for t in terms.values():
        baggage = t.get("baggage", {})
        if baggage.get("count", 0) < 1:
            return False
        if min_baggage_weight_kg is not None:
            weight = baggage.get("weight")
            if weight is None or weight < min_baggage_weight_kg:
                return False
    return True


def _select_proposal(
    ticket: dict, baggage_required: bool, min_baggage_weight_kg: Optional[int],
) -> Optional[dict]:
    """Выбирает самое дешёвое подходящее предложение билета — «подходящее»
    учитывает и `baggage_required`, и `min_baggage_weight_kg`
    (см. `_proposal_qualifies`)."""
    best = None
    best_price = None
    for p in ticket.get("proposals", []):
        if not _proposal_qualifies(p, baggage_required, min_baggage_weight_kg):
            continue
        val = p.get("price", {}).get("value")
        if val is None:
            continue
        if best_price is None or val < best_price:
            best_price = val
            best = p
    return best


def _min_leg_baggage_weight(p: dict) -> Optional[int]:
    """Минимальный по плечам вес разрешённого багажа выбранного предложения;
    плечи без багажа (`baggage.count < 1`) или без указанного веса в расчёт
    не берутся; None — ни на одном плече веса нет (неизвестен/багажа нет)."""
    weights = [
        t["baggage"]["weight"]
        for t in p.get("flight_terms", {}).values()
        if t.get("baggage", {}).get("count", 0) >= 1
        and t.get("baggage", {}).get("weight") is not None
    ]
    return min(weights) if weights else None


def _availability(p: dict, key: str) -> Optional[bool]:
    """Агрегирует `additional_tariff_info.<key>.available` по всем плечам
    выбранного предложения: результат True/False только если поле
    присутствует на КАЖДОМ плече, иначе None (данные неизвестны — напр.
    у продавца этот блок вообще не пришёл)."""
    values = []
    for t in p.get("flight_terms", {}).values():
        info = t.get("additional_tariff_info")
        if not isinstance(info, dict) or key not in info:
            return None
        avail = info[key].get("available")
        if avail is None:
            return None
        values.append(avail)
    if not values:
        return None
    return all(values)


def _has_baggage_option(ticket: dict) -> bool:
    for p in ticket.get("proposals", []):
        terms = p.get("flight_terms", {})
        if terms and all(t.get("baggage", {}).get("count", 0) >= 1 for t in terms.values()):
            return True
    return False


def extract_tickets(
    resp, baggage_required: bool = False, min_baggage_weight_kg: Optional[int] = None,
) -> list[Ticket]:
    out: list[Ticket] = []
    for chunk in _chunks(resp):
        flight_legs = chunk.get("flight_legs", [])
        airlines = chunk.get("airlines", {})
        for t in chunk.get("tickets", []):
            proposal = _select_proposal(t, baggage_required, min_baggage_weight_kg)
            if proposal is None:  # напр. нет предложения, подходящего под багаж
                continue
            price = proposal.get("price", {}).get("value")
            if price is None:
                continue
            directions = []
            for seg in t.get("segments", []):
                legs = [_leg(flight_legs[i], airlines) for i in seg.get("flights", [])]
                if not legs:
                    break
                directions.append(DirectionResult(legs=legs))
            if not directions:
                continue
            out.append(Ticket(
                price_rub=int(round(price)), directions=directions,
                has_baggage=_has_baggage_option(t), deep_link="",
                signature=str(t.get("signature") or t.get("id") or ""),
                baggage_weight_kg=_min_leg_baggage_weight(proposal),
                agent_id=proposal.get("agent_id"),
                changeable=_availability(proposal, "change_before_flight"),
                refundable=_availability(proposal, "return_before_flight"),
            ))
    return out
