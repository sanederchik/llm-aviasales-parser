import datetime as dt

from aviasales_search.cache import ProbeCache
from aviasales_search.two_phase import TwoPhasePlanner
from aviasales_search.trip_model import (
    CacheCfg, Constraints, DateWindow, Direction, DirectionResult, FlightLeg,
    Itinerary, Passengers, SearchBudget, Ticket,
)


def _ts(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


class _FakeClient:
    """One-way (1 напр.) — билет на дату; multi-city (2 напр.) — связка."""
    def __init__(self):
        self.one_way = 0
        self.multi = 0

    def search(self, dated_directions, passengers, trip_class, market_code,
               currency_code, baggage_required=False, min_baggage_weight_kg=None,
               filters_state=None):
        dirs = []
        for o, d, date_iso in dated_directions:
            dep = dt.datetime.fromisoformat(f"{date_iso}T10:00:00")
            arr = dep + dt.timedelta(hours=3)
            dirs.append(DirectionResult(legs=[FlightLeg(o, d, dep, arr, "TK", "TK1", _ts(dep), _ts(arr))]))
        if len(dated_directions) == 1:
            self.one_way += 1
        else:
            self.multi += 1
        total = sum(int(x[2][-2:]) for x in dated_directions) * 1000
        return [Ticket(price_rub=total, directions=dirs, has_baggage=True,
                       deep_link="", signature="s", baggage_weight_kg=20)]


def _config():
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y", passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 11))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 15), dt.date(2026, 9, 16))),
        ],
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints(), Constraints()],
        search_budget=SearchBudget(max_requests=100), cache=CacheCfg(ttl_minutes=60),
    )


def test_two_phase_sweeps_then_verifies_top_n(tmp_path):
    client = _FakeClient()
    cache = ProbeCache(tmp_path / "p.jsonl")
    captured = {}

    def on_legs_ready(leg_prices, ranked):
        captured["ranked"] = ranked

    tickets = TwoPhasePlanner(
        _config(), client, cache, now=dt.datetime(2026, 8, 8), verify_top=2,
        on_legs_ready=on_legs_ready,
    ).plan()

    assert client.one_way == 4  # 2+2 даты, полный свип
    assert client.multi == 2    # верифицированы только top-2 комбо
    assert "ranked" in captured and len(captured["ranked"]) >= 2
    assert tickets == sorted(tickets, key=lambda t: t.price_rub)
