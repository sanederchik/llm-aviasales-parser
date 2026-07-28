"""Markdown-отчёт по списку `Ticket` (каждый уже — вся поездка целиком, со всеми
направлениями). Отчёт показывает топ-N самых дешёвых билетов; на билет — таблица
строк по направлениям (одна строка = один `DirectionResult`)."""

from __future__ import annotations

import os
from pathlib import Path

from .trip_model import DirectionResult, Ticket


def _spaced(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _fmt_rub(n: int) -> str:
    return _spaced(n) + " ₽"


def _fmt_hm(minutes: int) -> str:
    return f"{minutes // 60}ч{minutes % 60:02d}м"


def _direction_route(d: DirectionResult) -> str:
    return f"{d.legs[0].origin}→{d.legs[-1].destination}"


def _direction_transfer_cell(d: DirectionResult) -> str:
    return ",".join(d.transfer_airports) if d.transfer_airports else "—"


def _direction_labels(n: int) -> list[str]:
    """Подписи направлений: для типового «туда-обратно» — привычные «Туда»/
    «Обратно» (как в живом эталонном отчёте); иначе — нумерация по порядку."""
    if n == 2:
        return ["Туда", "Обратно"]
    return [f"Направление {i + 1}" for i in range(n)]


def itinerary_to_offer_dict(t: Ticket) -> dict:
    return {
        "price_rub": t.price_rub,
        "has_baggage": t.has_baggage,
        "deep_link": t.deep_link,
        "route": t.route,
        "directions": [
            {
                "route": _direction_route(d),
                "depart": d.depart.isoformat(),
                "arrive": d.arrive.isoformat(),
                "transfers": d.transfers,
                "transfer_airports": d.transfer_airports,
                "duration_minutes": d.duration_minutes,
                "carrier": d.main_carrier_name,
            }
            for d in t.directions
        ],
    }


def offers_sorted_desc(tickets: list[Ticket]) -> list[dict]:
    dicts = [itinerary_to_offer_dict(t) for t in tickets]
    dicts.sort(key=lambda d: d["price_rub"], reverse=True)
    return dicts


def _render_direction_row(label: str, d: DirectionResult) -> str:
    date_cell = f"{d.depart:%Y-%m-%d %H:%M} → {d.arrive:%Y-%m-%d %H:%M}"
    return (
        f"| {label} | {date_cell} | {_direction_route(d)} | "
        f"{_direction_transfer_cell(d)} | {_fmt_hm(d.duration_minutes)} | "
        f"{d.main_carrier_name} |"
    )


def _render_one(idx: int, t: Ticket, subtitle: str) -> str:
    lines = [f"## Вариант {idx} — {_fmt_rub(t.price_rub)}{subtitle}", ""]
    lines.append("| Направление | Дата | Маршрут | Пересадка | В пути | Перевозчик |")
    lines.append("|---|---|---|---|---|---|")
    labels = _direction_labels(len(t.directions))
    for label, d in zip(labels, t.directions):
        lines.append(_render_direction_row(label, d))
    lines.append("")
    if t.deep_link:
        lines.append(f"[Открыть на aviasales]({t.deep_link})")
        lines.append("")
    return "\n".join(lines)


def _delta_header(cheapest_rub: int, previous_offers: list[dict]) -> list[str]:
    prev_best = min(o["price_rub"] for o in previous_offers)
    delta = cheapest_rub - prev_best
    sign = "−" if delta < 0 else "+"
    return [
        f"Лучшая цена: {_fmt_rub(cheapest_rub)} "
        f"(было {_fmt_rub(prev_best)}, {sign}{_spaced(abs(delta))})",
        "",
    ]


def render_markdown(tickets: list[Ticket], top_n: int = 10,
                    previous_offers: list[dict] | None = None) -> str:
    if not tickets:
        return "# Результаты поиска\n\nПодходящих вариантов не найдено.\n"

    cheapest_first = sorted(tickets, key=lambda t: t.price_rub)
    shown = cheapest_first[:top_n]

    header = ["# Результаты поиска Aviasales", ""]
    if previous_offers:
        header += _delta_header(shown[0].price_rub, previous_offers)

    blocks = [
        _render_one(i + 1, t, subtitle=" (лучший по цене)" if i == 0 else "")
        for i, t in enumerate(shown)
    ]
    return "\n".join(header) + "\n" + "\n".join(blocks)


class LiveReportWriter:
    """«Живой» отчёт: перезаписывает файл отчёта по ходу прогона, но только
    когда реально изменился топ-N (цена/маршрут/ссылка). Замена файла
    атомарная (tmp + os.replace), чтобы читатель не увидел полузаписанный
    отчёт."""

    def __init__(self, out_path, top_n: int,
                 previous_offers: list[dict] | None = None):
        self.out_path = Path(out_path)
        self.top_n = top_n
        self.previous_offers = previous_offers
        self._top_signature: list[tuple] | None = None

    def _signature(self, tickets: list[Ticket]) -> list[tuple]:
        top = sorted(tickets, key=lambda t: t.price_rub)[: self.top_n]
        return [(t.price_rub, t.signature, t.deep_link) for t in top]

    def update(self, tickets: list[Ticket]) -> None:
        signature = self._signature(tickets)
        if signature == self._top_signature:
            return
        self._top_signature = signature
        rendered = render_markdown(tickets, top_n=self.top_n,
                                   previous_offers=self.previous_offers)
        tmp = self.out_path.with_name(self.out_path.name + ".tmp")
        tmp.write_text(rendered, encoding="utf-8")
        os.replace(tmp, self.out_path)
