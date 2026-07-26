"""End-to-end regression guard against the live-verified etalon (report.md):
MOW<->DPS, 15.09/15.12, 2 adults, filters (baggage required, <=1 transfer,
<=24h duration, no Persian-Gulf transfers) -> best 160 423 RUB via CAN.

This was the missing test that let Fix 1's timezone bug (naive-local-datetime
duration inflation) silently reject the etalon ticket via the <=24h filter
(0 matches instead of 1) - see .sdd/live/analyze_reference.py for the
independently-verified reference logic this reproduces.
"""
import json
from pathlib import Path

from aviasales_search.filters import apply_filters
from aviasales_search.results_parser import extract_tickets
from aviasales_search.trip_model import GULF_AIRPORTS, Constraints

FIXTURE = Path(__file__).parent / "fixtures" / "results_v32_sample.json"


def test_etalon_constraints_yield_single_160423_ticket_via_can():
    resp = json.loads(FIXTURE.read_text())
    tickets = extract_tickets(resp, baggage_required=True)

    constraints = Constraints(
        max_transfers=1,
        max_duration_minutes=1440,
        baggage_required=True,
        exclude_transfer_airports=set(GULF_AIRPORTS),
    )
    survivors = apply_filters(tickets, constraints)

    assert len(survivors) == 1
    ticket = survivors[0]
    assert ticket.price_rub == 160423
    assert "CAN" in ticket.directions[0].transfer_airports
