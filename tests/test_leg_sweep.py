import datetime as dt

from aviasales_search.cache import ProbeCache
from aviasales_search.leg_sweep import LegSweeper, LegPrices, leg_itinerary
from aviasales_search.trip_model import (
    CacheCfg, Constraints, DateWindow, Direction, DirectionResult, FlightLeg,
    Itinerary, Passengers, SearchBudget, Ticket,
)


def _ts(d):
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _one_way(o, d, date_iso, price, dep_hour=10):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr = dep + dt.timedelta(hours=3)
    leg = FlightLeg(o, d, dep, arr, "TK", "TK1", _ts(dep), _ts(arr))
    return Ticket(price_rub=price, directions=[DirectionResult(legs=[leg])],
                  has_baggage=True, deep_link="", signature="s", baggage_weight_kg=20)


class _FakeClient:
    """Возвращает по одному one-way билету на дату; цена = day-of-month*1000."""
    def __init__(self):
        self.calls = []

    def search(self, dated_directions, passengers, trip_class, market_code,
               currency_code, baggage_required=False, min_baggage_weight_kg=None,
               filters_state=None):
        self.calls.append(tuple(dated_directions))
        (o, d, date_iso), = dated_directions
        day = int(date_iso[-2:])
        return [_one_way(o, d, date_iso, price=day * 1000)]


def _config():
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y",
        passengers=Passengers(adults=2),
        directions=[
            Direction("MOW", "ALA", DateWindow(dt.date(2026, 9, 10), dt.date(2026, 9, 12))),
            Direction("ALA", "TAS", DateWindow(dt.date(2026, 9, 15), dt.date(2026, 9, 16))),
        ],
        global_constraints=Constraints(),
        per_direction_constraints=[Constraints(), Constraints()],
        search_budget=SearchBudget(), cache=CacheCfg(ttl_minutes=60),
    )


def test_sweep_covers_all_dates_and_caches(tmp_path):
    client = _FakeClient()
    cache = ProbeCache(tmp_path / "probes.jsonl")
    now = dt.datetime(2026, 8, 8, 12, 0, 0)
    prices = LegSweeper(_config(), client, cache, now=now).sweep()

    assert prices.dates_with_tickets(0) == [dt.date(2026, 9, 10), dt.date(2026, 9, 11), dt.date(2026, 9, 12)]
    assert prices.dates_with_tickets(1) == [dt.date(2026, 9, 15), dt.date(2026, 9, 16)]
    assert prices.min_price(0, dt.date(2026, 9, 11)) == 11000
    assert len(client.calls) == 5  # 3 + 2 дат, все через сеть

    # повторный свип — всё из кэша, сеть не дёргается
    client2 = _FakeClient()
    LegSweeper(_config(), client2, cache, now=now).sweep()
    assert client2.calls == []


def test_leg_itinerary_strips_positional_time_of_day_for_middle_leg():
    from aviasales_search.trip_model import TimeOfDay
    cfg = _config()
    cfg.directions.append(Direction("TAS", "DPS", DateWindow(dt.date(2026, 10, 1), dt.date(2026, 10, 2))))
    cfg.per_direction_constraints.append(Constraints())
    cfg.global_constraints = Constraints(
        depart_time_of_day=[TimeOfDay.MORNING], arrive_time_of_day=[TimeOfDay.EVENING])
    # среднее плечо (i=1): ни depart, ни arrive tod из global не применяются
    sub = leg_itinerary(cfg, 1)
    assert sub.global_constraints.depart_time_of_day is None
    assert sub.global_constraints.arrive_time_of_day is None
    # первое плечо сохраняет depart tod, последнее — arrive tod
    assert leg_itinerary(cfg, 0).global_constraints.depart_time_of_day is not None
    assert leg_itinerary(cfg, 2).global_constraints.arrive_time_of_day is not None


class _TransferOneWayClient:
    """Каждый one-way результат — с пересадкой (2 лега)."""
    def search(self, dated_directions, passengers, trip_class, market_code,
               currency_code, baggage_required=False, min_baggage_weight_kg=None,
               filters_state=None):
        (o, d, date_iso), = dated_directions
        dep = dt.datetime.fromisoformat(f"{date_iso}T10:00:00")
        mid = dep + dt.timedelta(hours=3)
        dep2 = mid + dt.timedelta(hours=2)
        arr = dep2 + dt.timedelta(hours=3)
        legs = [FlightLeg(o, "IST", dep, mid, "TK", "1", _ts(dep), _ts(mid)),
                FlightLeg("IST", d, dep2, arr, "TK", "2", _ts(dep2), _ts(arr))]
        return [Ticket(price_rub=40000, directions=[DirectionResult(legs=legs)],
                       has_baggage=True, deep_link="", signature="s", baggage_weight_kg=20)]


def test_sweep_drops_tickets_failing_per_direction_filter(tmp_path):
    """Плечо с max_transfers=0, а билет с пересадкой -> дата пуста в LegPrices."""
    from aviasales_search.trip_model import Constraints as C
    cfg = _config()
    cfg.per_direction_constraints = [C(max_transfers=0), C(max_transfers=0)]
    prices = LegSweeper(cfg, _TransferOneWayClient(), ProbeCache(tmp_path / "p.jsonl"),
                        now=dt.datetime(2026, 8, 8)).sweep()
    assert prices.dates_with_tickets(0) == []  # все билеты отсеяны фильтром
    assert prices.dates_with_tickets(1) == []


class _RecordingReporter:
    def __init__(self):
        self.events = []

    def start(self, total, budget, ttl_minutes):
        pass

    def emit(self, event):
        self.events.append(event)


def test_sweep_progress_source_network_then_cache(tmp_path):
    """Прогресс Фазы 1 должен показывать «сеть» на свежих пробах и «кэш» на
    повторном свипе (регрессия: раньше источник был захардкожен «сеть»)."""
    cache = ProbeCache(tmp_path / "p.jsonl")
    now = dt.datetime(2026, 8, 8, 12, 0)
    r1 = _RecordingReporter()
    LegSweeper(_config(), _FakeClient(), cache, now=now, progress=r1).sweep()
    assert {e.source for e in r1.events} == {"сеть"}
    r2 = _RecordingReporter()
    LegSweeper(_config(), _FakeClient(), cache, now=now, progress=r2).sweep()
    assert {e.source for e in r2.events} == {"кэш"}
