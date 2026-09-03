import datetime as dt

from aviasales_search.cache import ProbeCache
from aviasales_search.ranking import RankedCombo
from aviasales_search.verifier import ComboVerifier
from aviasales_search.trip_model import (
    CacheCfg, Constraints, DateWindow, Direction, DirectionResult, FlightLeg,
    Itinerary, Passengers, SearchBudget, Ticket,
)


def _ts(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _combo_ticket(dates, price):
    dirs = []
    for (o, d), date in zip([("MOW", "ALA"), ("ALA", "TAS")], dates):
        dep = dt.datetime.fromisoformat(f"{date.isoformat()}T10:00:00")
        arr = dep + dt.timedelta(hours=3)
        dirs.append(DirectionResult(legs=[FlightLeg(o, d, dep, arr, "TK", "TK1", _ts(dep), _ts(arr))]))
    return Ticket(price_rub=price, directions=dirs, has_baggage=True, deep_link="",
                  signature="sig", baggage_weight_kg=20)


class _FakeClient:
    def __init__(self):
        self.calls = []

    def search(self, dated_directions, passengers, trip_class, market_code,
               currency_code, baggage_required=False, min_baggage_weight_kg=None,
               filters_state=None):
        self.calls.append(tuple(dated_directions))
        dates = [dt.date.fromisoformat(x[2]) for x in dated_directions]
        return [_combo_ticket(dates, price=sum(d.day for d in dates) * 1000)]


def _config():
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y", passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 12))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 15), dt.date(2026, 9, 17))),
        ],
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints(), Constraints()],
        search_budget=SearchBudget(max_requests=100), cache=CacheCfg(ttl_minutes=60),
    )


def _ranked(pairs):
    out = []
    for (a, b), total in pairs:
        out.append(RankedCombo(combo=(dt.date.fromisoformat(a), dt.date.fromisoformat(b)),
                               per_leg_min=(0, 0), total=total))
    return out


def test_verify_checks_only_top_n(tmp_path):
    client = _FakeClient()
    cache = ProbeCache(tmp_path / "p.jsonl")
    ranked = _ranked([
        (("2026-09-10", "2026-09-15"), 10),
        (("2026-09-11", "2026-09-16"), 20),
        (("2026-09-12", "2026-09-17"), 30),
    ])
    tickets = ComboVerifier(_config(), client, cache, now=dt.datetime(2026, 8, 8)).verify(ranked, top_n=2)
    assert len(client.calls) == 2  # только первые 2 комбо ранкинга
    assert tickets == sorted(tickets, key=lambda t: t.price_rub)


def test_verify_respects_max_network_ceiling(tmp_path):
    client = _FakeClient()
    cache = ProbeCache(tmp_path / "p.jsonl")
    ranked = _ranked([
        (("2026-09-10", "2026-09-15"), 10),
        (("2026-09-11", "2026-09-16"), 20),
        (("2026-09-12", "2026-09-17"), 30),
    ])
    ComboVerifier(_config(), client, cache, now=dt.datetime(2026, 8, 8)).verify(
        ranked, top_n=3, max_network=1)
    assert len(client.calls) == 1  # жёсткий потолок сети ниже top_n


def test_verify_cache_hits_do_not_count_against_ceiling(tmp_path):
    cache = ProbeCache(tmp_path / "p.jsonl")
    now = dt.datetime(2026, 8, 8)
    ranked = _ranked([
        (("2026-09-10", "2026-09-15"), 10),
        (("2026-09-11", "2026-09-16"), 20),
    ])
    # первый прогон наполняет кэш
    ComboVerifier(_config(), _FakeClient(), cache, now=now).verify(ranked, top_n=2)
    # второй: обе из кэша, плюс сетевой потолок=1 не мешает (кэш не тратит бюджет)
    client2 = _FakeClient()
    tickets = ComboVerifier(_config(), client2, cache, now=now).verify(ranked, top_n=2, max_network=1)
    assert client2.calls == []
    assert len(tickets) == 2


# --------------------------- миграция покрытия из Planner ---------------------------

from aviasales_search.verifier import baggage_required_for, min_baggage_weight_for


def _cfg_with(global_c=None, per_dir=None):
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y", passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 10))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 15), dt.date(2026, 9, 15))),
        ],
        global_constraints=global_c or Constraints(),
        per_direction_constraints=per_dir or [Constraints(), Constraints()],
        search_budget=SearchBudget(max_requests=100), cache=CacheCfg(ttl_minutes=60),
    )


def test_baggage_required_for_true_when_any_direction_requires():
    cfg = _cfg_with(per_dir=[Constraints(), Constraints(baggage_required=True)])
    assert baggage_required_for(cfg) is True


def test_baggage_required_for_false_when_none_requires():
    assert baggage_required_for(_cfg_with()) is False


def test_min_baggage_weight_for_is_max_over_directions():
    cfg = _cfg_with(per_dir=[Constraints(baggage_min_weight_kg=20),
                             Constraints(baggage_min_weight_kg=25)])
    assert min_baggage_weight_for(cfg) == 25


def test_min_baggage_weight_for_none_when_unset():
    assert min_baggage_weight_for(_cfg_with()) is None


class _TransferClient:
    """Возвращает связку, где второе плечо с пересадкой (2 лега)."""
    def search(self, dated_directions, passengers, trip_class, market_code,
               currency_code, baggage_required=False, min_baggage_weight_kg=None,
               filters_state=None):
        d0date, d1date = (dt.date.fromisoformat(x[2]) for x in dated_directions)
        dep0 = dt.datetime.fromisoformat(f"{d0date.isoformat()}T10:00:00")
        arr0 = dep0 + dt.timedelta(hours=3)
        leg0 = FlightLeg("MOW", "ALA", dep0, arr0, "TK", "1", _ts(dep0), _ts(arr0))
        dep1 = dt.datetime.fromisoformat(f"{d1date.isoformat()}T10:00:00")
        mid = dep1 + dt.timedelta(hours=3)
        dep1b = mid + dt.timedelta(hours=2)
        arr1 = dep1b + dt.timedelta(hours=3)
        legA = FlightLeg("ALA", "IST", dep1, mid, "TK", "2", _ts(dep1), _ts(mid))
        legB = FlightLeg("IST", "TAS", dep1b, arr1, "TK", "3", _ts(dep1b), _ts(arr1))
        return [Ticket(price_rub=50000,
                       directions=[DirectionResult(legs=[leg0]),
                                   DirectionResult(legs=[legA, legB])],
                       has_baggage=True, deep_link="", signature="s", baggage_weight_kg=20)]


def test_verify_excludes_tickets_violating_passes_itinerary(tmp_path):
    # Второе направление max_transfers=0, но билет имеет пересадку -> отсекается.
    cfg = _cfg_with(per_dir=[Constraints(max_transfers=0), Constraints(max_transfers=0)])
    ranked = _ranked([(("2026-09-10", "2026-09-15"), 1)])
    got = ComboVerifier(cfg, _TransferClient(), ProbeCache(tmp_path / "p.jsonl"),
                        now=dt.datetime(2026, 8, 8)).verify(ranked, top_n=1)
    assert got == []


def test_verify_refresh_forces_network_despite_cache(tmp_path):
    cache = ProbeCache(tmp_path / "p.jsonl")
    now = dt.datetime(2026, 8, 8)
    ranked = _ranked([(("2026-09-10", "2026-09-15"), 1)])
    ComboVerifier(_config(), _FakeClient(), cache, now=now).verify(ranked, top_n=1)
    client = _FakeClient()
    ComboVerifier(_config(), client, cache, now=now, refresh=True).verify(ranked, top_n=1)
    assert len(client.calls) == 1  # refresh игнорирует кэш


def test_verify_populates_deep_link_share_url(tmp_path):
    ranked = _ranked([(("2026-09-10", "2026-09-15"), 1)])
    got = ComboVerifier(_config(), _FakeClient(), ProbeCache(tmp_path / "p.jsonl"),
                        now=dt.datetime(2026, 8, 8)).verify(ranked, top_n=1)
    assert got and got[0].deep_link.startswith("https://www.aviasales.ru/search/")


class _RecordingReporter:
    def __init__(self):
        self.started = None
        self.events = []

    def start(self, total, budget, ttl_minutes):
        self.started = (total, budget, ttl_minutes)

    def emit(self, event):
        self.events.append(event)


def test_verify_reports_progress_start_and_event_per_combo(tmp_path):
    reporter = _RecordingReporter()
    ranked = _ranked([
        (("2026-09-10", "2026-09-15"), 10),
        (("2026-09-11", "2026-09-16"), 20),
    ])
    ComboVerifier(_config(), _FakeClient(), ProbeCache(tmp_path / "p.jsonl"),
                  now=dt.datetime(2026, 8, 8), progress=reporter).verify(ranked, top_n=2)
    assert reporter.started[0] == 2  # total = проверяемых комбо
    assert len(reporter.events) == 2
    assert reporter.events[0].improved is True  # первый прошедший билет — улучшение


def test_verify_different_baggage_weight_does_not_reuse_cache(tmp_path):
    """Кэш-ключ включает вес багажа: конфиги, различающиеся только
    baggage_min_weight_kg, не должны переиспользовать пробы друг друга
    (иначе выпадение суффикса из ключа осталось бы незамеченным)."""
    cache = ProbeCache(tmp_path / "p.jsonl")
    now = dt.datetime(2026, 8, 8)
    ranked = _ranked([(("2026-09-10", "2026-09-15"), 1)])
    cfg20 = _cfg_with(global_c=Constraints(baggage_min_weight_kg=20))
    cfg25 = _cfg_with(global_c=Constraints(baggage_min_weight_kg=25))
    ComboVerifier(cfg20, _FakeClient(), cache, now=now).verify(ranked, top_n=1)
    client = _FakeClient()
    ComboVerifier(cfg25, client, cache, now=now).verify(ranked, top_n=1)
    assert len(client.calls) == 1  # другой вес -> другой ключ -> свежий запрос
