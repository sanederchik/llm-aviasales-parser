import datetime as dt

from aviasales_search.ranking import (
    RankedCombo, combos_from_date_lists, rank_combos, _passes_stay_days,
)
from aviasales_search.trip_model import (
    CacheCfg, Constraints, DateWindow, Direction, Itinerary, Passengers,
    SearchBudget, StayDays,
)


def _itin(directions, per_dir=None, max_trip_days=None):
    return Itinerary(
        currency="rub", market_code="ru", trip_class="Y",
        passengers=Passengers(adults=2), directions=directions,
        global_constraints=Constraints(),
        per_direction_constraints=per_dir or [Constraints() for _ in directions],
        search_budget=SearchBudget(), cache=CacheCfg(), max_trip_days=max_trip_days,
    )


def _dir(o, d, e, l, stay=None):
    return Direction(o, d, DateWindow(dt.date.fromisoformat(e), dt.date.fromisoformat(l)), stay_days=stay)


class _FakeLegPrices:
    """Мин. цены по (плечо, дата); только даты с ценой считаются валидными."""
    def __init__(self, prices):  # prices: list[dict[date, int]]
        self._prices = prices

    def dates_with_tickets(self, i):
        return sorted(self._prices[i])

    def min_price(self, i, d):
        return self._prices[i].get(d)


def test_rank_combos_sorted_by_total_and_respects_stay_days():
    d = dt.date.fromisoformat
    directions = [
        _dir("MOW", "ALA", "2026-09-10", "2026-09-12", stay=StayDays(min_days=5, max_days=7)),
        _dir("ALA", "TAS", "2026-09-15", "2026-09-19"),
    ]
    prices = [
        {d("2026-09-10"): 50000, d("2026-09-11"): 44000},
        {d("2026-09-16"): 20000, d("2026-09-17"): 17000},
    ]
    ranked = rank_combos(_itin(directions), _FakeLegPrices(prices))
    # stay_days 5..7: с 10.09 разрешены 15..17 -> 16,17; с 11.09 -> 16,17,18(нет цены)->16,17
    totals = [r.total for r in ranked]
    assert totals == sorted(totals)
    assert ranked[0] == RankedCombo(
        combo=(d("2026-09-11"), d("2026-09-17")), per_leg_min=(44000, 17000), total=61000,
    )
    # монотонность/пропуски: комбо с датой без цены не появляется
    assert all(r.per_leg_min[0] is not None and r.per_leg_min[1] is not None for r in ranked)


def test_combos_respect_monotonicity_and_max_trip_days():
    d = dt.date.fromisoformat
    directions = [_dir("MOW", "ALA", "2026-09-10", "2026-09-12"),
                  _dir("ALA", "TAS", "2026-09-10", "2026-09-20")]
    per_dir_dates = [[d("2026-09-10"), d("2026-09-12")],
                     [d("2026-09-09"), d("2026-09-11"), d("2026-09-25")]]
    # монотонность: combo[1] >= combo[0]; max_trip_days=3 отбрасывает дальние
    combos = combos_from_date_lists(_itin(directions, max_trip_days=3), per_dir_dates)
    assert (d("2026-09-10"), d("2026-09-09")) not in combos  # немонотонно
    assert (d("2026-09-10"), d("2026-09-25")) not in combos  # > max_trip_days
    assert (d("2026-09-10"), d("2026-09-11")) in combos


def test_passes_stay_days_boundaries():
    d = dt.date.fromisoformat
    directions = [_dir("MOW", "ALA", "2026-09-10", "2026-09-10", stay=StayDays(min_days=5, max_days=7)),
                  _dir("ALA", "TAS", "2026-09-15", "2026-09-20")]
    itin = _itin(directions)
    assert _passes_stay_days(itin, (d("2026-09-10"), d("2026-09-15")))  # ровно 5
    assert _passes_stay_days(itin, (d("2026-09-10"), d("2026-09-17")))  # ровно 7
    assert not _passes_stay_days(itin, (d("2026-09-10"), d("2026-09-14")))  # 4 < 5
    assert not _passes_stay_days(itin, (d("2026-09-10"), d("2026-09-18")))  # 8 > 7
