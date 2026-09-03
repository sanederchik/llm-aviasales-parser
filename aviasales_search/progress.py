"""Прогресс перебора: события от `LegSweeper` (Фаза 1) и `ComboVerifier`
(Фаза 2) и их запись в текстовые синки (stderr, файл прогона). Компоненты
остаются чистыми — они лишь вызывают `ProgressReporter.start/emit`; кто и куда
пишет строки, решает граница (`cli.py`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import IO, Optional, Protocol, Sequence

from .report import _fmt_rub


@dataclass
class ComboEvent:
    """Итог обработки одной комбинации дат.

    `source` — откуда билеты: "сеть", "кэш" или "пропуск" (сетевой бюджет
    исчерпан, комбинация не искалась — тогда счётчики/цена равны None).
    `best_price` — минимальная цена (₽) среди билетов, прошедших фильтры;
    None, если прошедших нет.
    `running_min` — лучшая цена с НАЧАЛА прогона (включая эту комбинацию);
    None, пока ни один билет не прошёл фильтры. `improved` — эта комбинация
    улучшила бегущий минимум (первый найденный билет — тоже улучшение)."""

    index: int
    total: int
    dated_dirs: Sequence[tuple[str, str, str]]
    source: str
    found: Optional[int]
    passed: Optional[int]
    best_price: Optional[int]
    running_min: Optional[int]
    improved: bool


class ProgressReporter(Protocol):
    def start(self, total: int, budget: int, ttl_minutes: int) -> None: ...

    def emit(self, event: ComboEvent) -> None: ...


def format_header(total: int, budget: int, ttl_minutes: int) -> str:
    return f"Комбинаций дат: {total}, сетевой бюджет: {budget}, кэш TTL: {ttl_minutes} мин"


def _opt(n: Optional[int]) -> str:
    return "—" if n is None else str(n)


def format_combo_line(event: ComboEvent) -> str:
    width = len(str(event.total))
    pct = event.index / event.total * 100 if event.total else 0.0
    prefix = f"[{event.index:>{width}}/{event.total} {pct:5.1f}%]"
    dirs = " | ".join(
        f"{origin}→{destination} {date_iso}"
        for origin, destination, date_iso in event.dated_dirs
    )
    price = _fmt_rub(event.best_price) if event.best_price is not None else "—"
    running = _fmt_rub(event.running_min) if event.running_min is not None else "—"
    star = " ★" if event.improved else ""
    return (
        f"{prefix} {dirs} | {event.source}"
        f" | найдено {_opt(event.found)} | прошло {_opt(event.passed)} | лучшая {price}"
        f" | мин. с начала {running}{star}"
    )


@dataclass
class ProgressLog:
    """Пишет заголовок и строки прогресса во все синки, флашит после каждой
    строки (иначе `tail -f` и вывод фоновых задач не видят прогресс).
    Закрытием потоков не управляет — ими владеет вызывающий код."""

    sinks: Sequence[IO[str]]

    def _write_line(self, line: str) -> None:
        for sink in self.sinks:
            sink.write(line + "\n")
            sink.flush()

    def start(self, total: int, budget: int, ttl_minutes: int) -> None:
        self._write_line(format_header(total, budget, ttl_minutes))

    def emit(self, event: ComboEvent) -> None:
        self._write_line(format_combo_line(event))
