"""Оркестратор двухфазного поиска: Фаза 1 (LegSweeper) → ранкинг (rank_combos)
→ коллбэк промежуточного отчёта → Фаза 2 (ComboVerifier). Файлов не пишет —
это делает граница (cli) через инжектированные коллбэки."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Optional

from .cache import ProbeCache
from .leg_sweep import LegPrices, LegSweeper
from .progress import ProgressReporter
from .ranking import RankedCombo, rank_combos
from .trip_model import Itinerary, Ticket
from .verifier import ComboVerifier


@dataclass
class TwoPhasePlanner:
    config: Itinerary
    client: object
    cache: ProbeCache
    now: dt.datetime
    verify_top: int
    refresh: bool = False
    progress: Optional[ProgressReporter] = None
    on_legs_ready: Optional[Callable[[LegPrices, list[RankedCombo]], None]] = None
    final_live_report: Optional[Callable[[list[Ticket]], None]] = None

    def plan(self) -> list[Ticket]:
        def on_leg(leg_prices: LegPrices) -> None:
            # Живой промежуточный отчёт после каждого плеча: ранкинг считаем на
            # накопленных данных (rank_combos дёшев и детерминирован).
            if self.on_legs_ready is not None:
                self.on_legs_ready(leg_prices, rank_combos(self.config, leg_prices))

        leg_prices = LegSweeper(
            self.config, self.client, self.cache, now=self.now,
            refresh=self.refresh, progress=self.progress,
            live_report=on_leg if self.on_legs_ready is not None else None,
        ).sweep()
        ranked = rank_combos(self.config, leg_prices)
        return ComboVerifier(
            self.config, self.client, self.cache, now=self.now,
            refresh=self.refresh, progress=self.progress,
            live_report=self.final_live_report,
        ).verify(ranked, top_n=self.verify_top,
                 max_network=self.config.search_budget.max_requests)
