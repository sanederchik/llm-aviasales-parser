import json
from pathlib import Path

import pytest

from aviasales_search.curl_auth import CurlAuth, ExpiredCurlError
from aviasales_search.search_client import (
    RESULTS_URL,
    START_URL,
    Response,
    SearchClient,
    build_start_body,
)
from aviasales_search.trip_model import Passengers

FIXTURE = Path(__file__).parent / "fixtures" / "results_v32_sample.json"

DATED_DIRECTIONS = [
    ("MOW", "DPS", "2026-09-15"),
    ("DPS", "MOW", "2026-12-15"),
]
PASSENGERS = {"adults": 2, "children": 0, "infants": 0}


def _auth():
    return CurlAuth(headers={"content-type": "application/json"}, cookies={"auid": "abc"})


def _full_results_text() -> str:
    # The fixture's single chunk already has meta.total_tickets_count == 4 ==
    # len(tickets) — i.e. it represents an already-complete poll.
    return FIXTURE.read_text()


def _empty_results_text() -> str:
    chunk = json.loads(FIXTURE.read_text())[0]
    chunk = {**chunk, "tickets": []}
    return json.dumps([chunk])


def _partial_results_text(n: int, total: int | None = None) -> str:
    """First `n` tickets of the fixture, with `meta.total_tickets_count`
    overridden to `total` (defaults to `n`, i.e. "this partial IS the whole
    truth" — used for reached-total tests where `total` is passed explicitly
    higher than `n` to simulate a still-filling search)."""
    chunk = json.loads(FIXTURE.read_text())[0]
    meta = dict(chunk.get("meta", {}))
    meta["total_tickets_count"] = total if total is not None else n
    chunk = {**chunk, "tickets": chunk["tickets"][:n], "meta": meta}
    return json.dumps([chunk])


class RecordingTransport:
    """Мок-транспорт: отдаёт заранее заданные ответы по порядку вызовов,
    записывая (method, url, headers, cookies, body) каждого."""

    def __init__(self, responses: list[Response]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def __call__(self, method, url, headers, cookies, body):
        self.calls.append((method, url, headers, cookies, body))
        return self._responses.pop(0)


def _client(transport, max_poll=6):
    return SearchClient(auth=_auth(), transport=transport, sleep=lambda s: None, max_poll=max_poll)


def _search(client, baggage_required=False):
    return client.search(
        dated_directions=DATED_DIRECTIONS, passengers=PASSENGERS, trip_class="Y",
        market_code="ru", currency_code="rub", baggage_required=baggage_required,
    )


# --------------------------- build_start_body ---------------------------


def test_build_start_body_shape():
    body = build_start_body(
        dated_directions=DATED_DIRECTIONS,
        passengers=PASSENGERS,
        trip_class="Y",
        market_code="ru",
        currency_code="rub",
    )
    assert body["search_params"]["directions"] == [
        {"origin": "MOW", "destination": "DPS", "date": "2026-09-15",
         "is_origin_airport": False, "is_destination_airport": False},
        {"origin": "DPS", "destination": "MOW", "date": "2026-12-15",
         "is_origin_airport": False, "is_destination_airport": False},
    ]
    assert body["search_params"]["passengers"] == PASSENGERS
    assert body["search_params"]["trip_class"] == "Y"
    assert body["market_code"] == "ru"
    assert body["marker"] == "direct"
    assert body["citizenship"] == "RU"
    assert body["currency_code"] == "rub"
    assert body["brand"] == "AS"
    assert isinstance(body["client_features"], dict)
    assert body["client_features"]["direct_flights"] is True
    assert body["filters"] == {"initial_state": {}}
    # Doc-verified extra fields (Correction from review: these are present in
    # every live-captured START body per docs/aviasales-api-v3.2.md).
    assert body["languages"] == {"ru": 1}
    assert body["debug"] == {"override_experiment_groups": {}}
    assert body["subscription_ticket_signatures"] == []
    assert body["is_internet_restricted"] is False
    # experiment_groups is explicitly optional per the doc — omitted here.
    assert "experiment_groups" not in body


def test_build_start_body_accepts_passengers_dataclass():
    # R6 planner passes a real trip_model.Passengers instance, not a dict.
    passengers = Passengers(adults=2, children=1, infants=1)
    body = build_start_body(
        dated_directions=DATED_DIRECTIONS,
        passengers=passengers,
        trip_class="Y",
        market_code="ru",
        currency_code="rub",
    )
    assert body["search_params"]["passengers"] == {"adults": 2, "children": 1, "infants": 1}


# --------------------------- SearchClient.search ---------------------------


def test_search_returns_tickets_from_fixture():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    results_resp = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp])
    sleeps = []

    client = SearchClient(auth=_auth(), transport=transport, sleep=sleeps.append, max_poll=6)
    tickets = _search(client)

    assert len(tickets) == 4
    # a sleep happened before each of the two network calls
    assert len(sleeps) == 2
    assert all(s > 0 for s in sleeps)


def test_search_hits_correct_urls_in_order():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    results_resp = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp])

    _search(_client(transport))

    assert transport.calls[0][0] == "POST"
    assert transport.calls[0][1] == START_URL
    assert transport.calls[1][0] == "POST"
    assert transport.calls[1][1] == RESULTS_URL


def test_search_polls_results_url_host_from_start_response():
    # Реальный START (live 2026-07-27) отдаёт хост результатов динамически:
    # {"results_url": "tickets-api.ru-central-1.aviasales.ru", ...} — регион
    # меняется от поиска к поиску, зашитый хост даёт 404 на поллинге.
    start_resp = Response(status=200, text=json.dumps({
        "search_id": "sid",
        "results_url": "tickets-api.ru-central-1.aviasales.ru",
    }))
    results_resp = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp])

    _search(_client(transport))

    assert transport.calls[1][1] == (
        "https://tickets-api.ru-central-1.aviasales.ru/search/v3.2/results"
    )


def test_start_body_contains_passed_directions_and_dates():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    results_resp = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp])

    _search(_client(transport))

    sent_body = json.loads(transport.calls[0][4])
    sent_directions = sent_body["search_params"]["directions"]
    assert [(d["origin"], d["destination"], d["date"]) for d in sent_directions] == DATED_DIRECTIONS


def test_results_body_references_search_id():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid-xyz"}))
    results_resp = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp])

    _search(_client(transport))

    sent_body = json.loads(transport.calls[1][4])
    assert sent_body == {
        "limit": 1000, "price_per_person": False, "search_by_airport": False,
        "filters_state": {}, "search_id": "sid-xyz", "last_update_timestamp": 0,
    }


def test_search_raises_expired_curl_on_403_at_start():
    start_resp = Response(status=403, text="")
    transport = RecordingTransport([start_resp])

    client = _client(transport)
    with pytest.raises(ExpiredCurlError):
        _search(client)
    assert len(transport.calls) == 1  # never reaches RESULTS


def test_search_raises_expired_curl_on_block_during_poll():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    blocked_resp = Response(status=429, text="")
    transport = RecordingTransport([start_resp, blocked_resp])

    client = _client(transport)
    with pytest.raises(ExpiredCurlError):
        _search(client)


def test_search_polls_up_to_max_poll_while_empty():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    empty_resp = Response(status=200, text=_empty_results_text())
    transport = RecordingTransport([start_resp] + [empty_resp] * 3)

    tickets = _search(_client(transport, max_poll=3))

    assert tickets == []
    # 1 START call + max_poll RESULTS calls, all exhausted
    assert len(transport.calls) == 1 + 3


def test_search_stops_early_once_ticket_count_reaches_meta_total():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    # Growing: 2 of 4, then 4 of 4 -> count reaches meta.total_tickets_count.
    growing = Response(status=200, text=_partial_results_text(2, total=4))
    full = Response(status=200, text=_partial_results_text(4, total=4))
    never_reached = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, growing, full, never_reached])

    tickets = _search(_client(transport, max_poll=6))

    assert len(tickets) == 4
    # START + 2 RESULTS polls only — stops once count == meta.total_tickets_count,
    # never touches the 3rd (never_reached) mocked response.
    assert len(transport.calls) == 3


def test_search_stops_early_once_two_consecutive_polls_yield_same_count():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    # meta.total_tickets_count says 10 (never reached), but the count itself
    # stops changing across two consecutive polls -> stabilization via (c).
    same1 = Response(status=200, text=_partial_results_text(2, total=10))
    same2 = Response(status=200, text=_partial_results_text(2, total=10))
    never_reached = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, same1, same2, never_reached])

    tickets = _search(_client(transport, max_poll=6))

    assert len(tickets) == 2
    # START + 2 RESULTS polls only (stopped before the 3rd mocked response).
    assert len(transport.calls) == 3


def test_search_returns_last_partial_result_when_max_poll_exhausted_without_stabilizing():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    # Count keeps growing (1, 2, 3) against a higher meta total (4) that's
    # never reached, and never repeats -> exhausts max_poll=3, returns last.
    p1 = Response(status=200, text=_partial_results_text(1, total=4))
    p2 = Response(status=200, text=_partial_results_text(2, total=4))
    p3 = Response(status=200, text=_partial_results_text(3, total=4))
    transport = RecordingTransport([start_resp, p1, p2, p3])

    tickets = _search(_client(transport, max_poll=3))

    assert len(tickets) == 3  # last (possibly partial) result, per spec
    assert len(transport.calls) == 1 + 3


def test_search_treats_304_as_no_change_and_stops_when_previous_result_exists():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    growing = Response(status=200, text=_partial_results_text(2, total=10))
    not_modified = Response(status=304, text="")
    never_reached = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, growing, not_modified, never_reached])

    tickets = _search(_client(transport, max_poll=6))

    assert len(tickets) == 2  # last known non-empty result, from `growing`
    # START + 1 non-empty poll + 1 "304 = no change" poll = 3 calls total.
    assert len(transport.calls) == 3


def test_search_keeps_polling_when_304_arrives_before_any_data():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    not_modified = Response(status=304, text="")
    full = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, not_modified, full])

    tickets = _search(_client(transport, max_poll=6))

    assert len(tickets) == 4
    # START + 1 (304, no prior data -> keep polling) + 1 (full, complete) = 3.
    assert len(transport.calls) == 3


def test_search_treats_empty_body_200_as_no_change_without_crashing():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    growing = Response(status=200, text=_partial_results_text(2, total=10))
    empty_body_200 = Response(status=200, text="")  # undecodable/empty, not 304
    transport = RecordingTransport([start_resp, growing, empty_body_200])

    tickets = _search(_client(transport, max_poll=6))

    assert len(tickets) == 2
    assert len(transport.calls) == 3


def test_search_applies_baggage_required_filter():
    start_resp = Response(status=200, text=json.dumps({"search_id": "sid"}))
    # baggage_required=True filters the fixture's 4 raw tickets down to 2 —
    # meta.total_tickets_count (4, raw/unfiltered) is never reached by the
    # *filtered* count, so stabilization here relies on two consecutive polls
    # yielding the same filtered count, not on reaching meta's raw total.
    results_resp_1 = Response(status=200, text=_full_results_text())
    results_resp_2 = Response(status=200, text=_full_results_text())
    transport = RecordingTransport([start_resp, results_resp_1, results_resp_2])

    tickets = _search(_client(transport, max_poll=6), baggage_required=True)

    assert len(tickets) == 2  # only baggage-inclusive tickets survive (see test_results_parser)
    assert all(t.has_baggage for t in tickets)
    assert len(transport.calls) == 3  # START + 2 identical polls -> stabilized


def test_response_json_parses_text():
    resp = Response(status=200, text='{"a": 1}')
    assert resp.json() == {"a": 1}


def test_default_transport_does_not_import_curl_cffi_at_module_level():
    import sys

    assert "curl_cffi" not in sys.modules
    import aviasales_search.search_client as sc

    assert "curl_cffi" not in sys.modules
    assert callable(sc.default_transport)
