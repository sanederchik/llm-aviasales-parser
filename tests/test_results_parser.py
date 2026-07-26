import json
from pathlib import Path

import pytest

from aviasales_search.report import itinerary_to_offer_dict, render_markdown
from aviasales_search.results_parser import extract_tickets
from aviasales_search.trip_model import Ticket

FIXTURE = Path(__file__).parent / "fixtures" / "results_v32_sample.json"


def _load():
    return json.loads(FIXTURE.read_text())


def test_extract_tickets_finds_all_four():
    resp = _load()
    tickets = extract_tickets(resp)
    assert len(tickets) == 4
    assert all(isinstance(t, Ticket) for t in tickets)


def _find_valid_ticket(tickets):
    # The "valid" ticket: cheapest, via Chinese hub (CAN), 1 transfer/direction,
    # has a baggage-inclusive fare option. Matches the live-verified best price
    # of ~160 423 RUB (see global-constraints.md traceability note).
    for t in tickets:
        if t.price_rub == 160423:
            return t
    raise AssertionError("valid ticket (160423 RUB) not found")


def test_valid_ticket_shape_and_price():
    tickets = extract_tickets(_load())
    valid = _find_valid_ticket(tickets)

    assert valid.price_rub == 160423
    assert valid.has_baggage is True
    assert len(valid.directions) == 2
    for direction in valid.directions:
        assert direction.transfers == 1
    assert "CAN" in valid.directions[0].transfer_airports
    assert "CAN" in valid.directions[1].transfer_airports


def test_ticket_signature_captured_for_share_link():
    # signature is what the share-URL `t=…_<sig>_<price>` tail addresses a ticket by
    tickets = extract_tickets(_load())
    assert all(t.signature for t in tickets)


def test_valid_ticket_duration_minutes_from_unix_timestamps():
    """duration_minutes must be computed from unix timestamps (departure_ts/
    arrival_ts), not by subtracting naive LOCAL datetimes - SVO and DPS are in
    different timezones, so a naive-datetime subtraction is inflated by the
    UTC-offset delta (would wrongly give 1585/740 instead of the correct
    1285/1040 - see .sdd/live/analyze_reference.py and report.md etalon:
    "В пути 21ч25м" out / "17ч20м" back = 1285 / 1040 minutes)."""
    tickets = extract_tickets(_load())
    valid = _find_valid_ticket(tickets)

    # direction 1: SVO 2026-09-15 21:15 -> DPS 2026-09-16 23:40 (via CAN)
    assert valid.directions[0].duration_minutes == 1285
    # direction 2: DPS 2026-12-15 00:40 -> SVO 2026-12-15 13:00 (via CAN)
    assert valid.directions[1].duration_minutes == 1040


def test_gulf_ticket_contains_auh_transfer_airport():
    tickets = extract_tickets(_load())
    gulf = [t for t in tickets if t.price_rub == 148186]
    assert len(gulf) == 1
    gulf_ticket = gulf[0]
    assert any("AUH" in d.transfer_airports for d in gulf_ticket.directions)
    # This one has no qualifying (all-legs) baggage-inclusive fare.
    assert gulf_ticket.has_baggage is False


def test_two_transfer_ticket_present():
    tickets = extract_tickets(_load())
    two_transfer = [t for t in tickets if t.price_rub == 127862]
    assert len(two_transfer) == 1
    ticket = two_transfer[0]
    assert any(d.transfers == 2 for d in ticket.directions)


def test_baggage_required_cuts_tickets_without_baggage_option():
    resp = _load()
    all_tickets = extract_tickets(resp, baggage_required=False)
    baggage_only = extract_tickets(resp, baggage_required=True)

    assert len(all_tickets) == 4
    # Only tickets with an all-legs baggage-inclusive proposal survive.
    assert all(t.has_baggage for t in baggage_only)
    assert len(baggage_only) < len(all_tickets)
    assert len(baggage_only) == 2


def test_baggage_required_price_may_differ_from_cheapest_overall():
    resp = _load()
    baggage_only = extract_tickets(resp, baggage_required=True)
    valid = _find_valid_ticket(baggage_only)
    assert valid.price_rub == 160423


def test_all_prices_positive():
    tickets = extract_tickets(_load())
    assert all(t.price_rub > 0 for t in tickets)


def test_valid_ticket_carrier_name_resolved_from_airlines_map():
    """The poll chunk's `airlines` map resolves IATA code "CZ" to its human
    name; report/offer output must show this, not the bare code (etalon:
    carrier "China Southern Airlines")."""
    tickets = extract_tickets(_load())
    valid = _find_valid_ticket(tickets)
    for direction in valid.directions:
        assert direction.main_carrier == "CZ"
        assert direction.main_carrier_name == "China Southern Airlines"


def test_valid_ticket_report_and_offer_show_airline_name_not_code():
    tickets = extract_tickets(_load(), baggage_required=True)
    valid = _find_valid_ticket(tickets)

    offer = itinerary_to_offer_dict(valid)
    assert all(d["carrier"] == "China Southern Airlines" for d in offer["directions"])

    md = render_markdown([valid])
    assert "China Southern Airlines" in md
