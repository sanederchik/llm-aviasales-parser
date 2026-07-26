from __future__ import annotations

import datetime as dt

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


def _cheapest_proposal_price(ticket: dict, baggage_required: bool):
    best = None
    for p in ticket.get("proposals", []):
        if baggage_required:
            terms = p.get("flight_terms", {})
            if not terms or not all(t.get("baggage", {}).get("count", 0) >= 1 for t in terms.values()):
                continue
        val = p.get("price", {}).get("value")
        if val is None:
            continue
        if best is None or val < best:
            best = val
    return best


def _has_baggage_option(ticket: dict) -> bool:
    for p in ticket.get("proposals", []):
        terms = p.get("flight_terms", {})
        if terms and all(t.get("baggage", {}).get("count", 0) >= 1 for t in terms.values()):
            return True
    return False


def extract_tickets(resp, baggage_required: bool = False) -> list[Ticket]:
    out: list[Ticket] = []
    for chunk in _chunks(resp):
        flight_legs = chunk.get("flight_legs", [])
        airlines = chunk.get("airlines", {})
        for t in chunk.get("tickets", []):
            price = _cheapest_proposal_price(t, baggage_required)
            if price is None:  # e.g. baggage_required and no qualifying fare
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
            ))
    return out
