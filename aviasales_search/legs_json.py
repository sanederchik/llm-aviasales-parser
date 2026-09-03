"""Промежуточный JSON-первоисточник Фазы 1: матрица поплечевых минимумов по
датам + ранкинг комбо. MD рендерится из этого JSON (legs_report), как принято
в проекте (JSON — источник правды)."""
from __future__ import annotations

import datetime as dt

from .leg_sweep import LegPrices
from .ranking import RankedCombo
from .trip_model import Itinerary

LEGS_SCHEMA_VERSION = 1


def _date_entry(date, ld) -> dict:
    """Запись по дате. ld is None -> дата свипнута, но вариантов не нашлось:
    `min_price_rub: null` (в MD рендерится «—»). Полное окно направления
    выводится всегда, поэтому «нет вариантов» отличимо от «дата не показана»."""
    if ld is None:
        return {"date": date.isoformat(), "min_price_rub": None}
    d = ld.best.directions[0]
    return {
        "date": date.isoformat(),
        "min_price_rub": ld.min_price,
        "carrier": d.main_carrier,
        "carrier_name": d.main_carrier_name,
        "transfers": d.transfers,
        "transfer_airports": d.transfer_airports,
        "deep_link": ld.best.deep_link,
    }


def _leg_entry(direction, by_date: dict) -> dict:
    # Все даты окна направления (не только с билетами) — чтобы «—» для пустых
    # дат был отличим от «даты нет в отчёте».
    return {
        "route": f"{direction.origin}→{direction.destination}",
        "origin": direction.origin,
        "destination": direction.destination,
        "dates": [_date_entry(d, by_date.get(d)) for d in direction.date_window.days()],
    }


def _ranking_entry(rc: RankedCombo) -> dict:
    stays = [(rc.combo[i + 1] - rc.combo[i]).days for i in range(len(rc.combo) - 1)]
    return {
        "combo": [d.isoformat() for d in rc.combo],
        "per_leg_min_rub": list(rc.per_leg_min),
        "stay_days": stays,
        "total_rub": rc.total,
    }


def legs_to_json(itinerary: Itinerary, leg_prices: LegPrices,
                 ranked: list[RankedCombo], trip: dict,
                 generated_at: dt.datetime) -> dict:
    return {
        "schema_version": LEGS_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "trip": trip,
        "legs": [
            _leg_entry(direction, leg_prices.per_direction[i])
            for i, direction in enumerate(itinerary.directions)
        ],
        "ranking": [_ranking_entry(rc) for rc in ranked],
    }
