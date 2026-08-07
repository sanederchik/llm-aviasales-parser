"""Markdown-отчёт по списку `Ticket` (каждый уже — вся поездка целиком, со всеми
направлениями). Билеты группируются по комбинации дат вылета (`ComboGroup`);
отчёт — плоская таблица, одна строка = одна комбинация дат (лучшая цена в
группе), топ-N комбинаций по цене."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .trip_model import DirectionResult, Itinerary, Ticket


def _spaced(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _fmt_rub(n: int) -> str:
    return _spaced(n) + " ₽"


def _fmt_hm(minutes: int) -> str:
    return f"{minutes // 60}ч{minutes % 60:02d}м"


def _direction_route(d: DirectionResult) -> str:
    return f"{d.legs[0].origin}→{d.legs[-1].destination}"


def combo_dates(t: Ticket) -> tuple[dt.date, ...]:
    """Первичный ключ строки отчёта: даты вылета каждого плеча. Планнер ищет
    по точным датам, поэтому кортеж однозначно задаёт комбинацию."""
    return tuple(d.depart.date() for d in t.directions)


@dataclass
class ComboGroup:
    """Все прошедшие фильтры билеты одной комбинации дат (по возрастанию цены)."""

    dates: tuple[dt.date, ...]
    tickets: list[Ticket]

    @property
    def best(self) -> Ticket:
        return self.tickets[0]

    @property
    def equal_alternatives(self) -> list[Ticket]:
        return [t for t in self.tickets[1:] if t.price_rub == self.best.price_rub]

    @property
    def pricier_alternatives(self) -> list[Ticket]:
        return [t for t in self.tickets[1:] if t.price_rub > self.best.price_rub][:3]


def group_tickets_by_combo(tickets: list[Ticket]) -> list[ComboGroup]:
    """Группирует билеты по комбинациям дат вылета, сортирует внутри группы по
    цене, затем сортирует сами группы по (цена лучшего, даты)."""
    by_key: dict[tuple[dt.date, ...], list[Ticket]] = {}
    for t in tickets:
        by_key.setdefault(combo_dates(t), []).append(t)
    groups = [
        ComboGroup(dates=key, tickets=sorted(ts, key=lambda t: t.price_rub))
        for key, ts in by_key.items()
    ]
    groups.sort(key=lambda g: (g.best.price_rub, g.dates))
    return groups


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


def direction_labels_for(itinerary: Itinerary) -> list[str]:
    """Заголовки колонок-плеч из конфига: привычные «Туда»/«Обратно» для
    туда-обратно, иначе маршрут плеча."""
    dirs = itinerary.directions
    if (len(dirs) == 2 and dirs[0].origin == dirs[1].destination
            and dirs[0].destination == dirs[1].origin):
        return ["Туда", "Обратно"]
    return [f"{d.origin}→{d.destination}" for d in dirs]


def _fallback_labels(n: int) -> list[str]:
    if n == 2:
        return ["Туда", "Обратно"]
    return [f"Плечо {i + 1}" for i in range(n)]


def _dates_cell(dates: tuple[dt.date, ...]) -> str:
    return " → ".join(f"{d:%d.%m}" for d in dates)


def _leg_cell(d: DirectionResult) -> str:
    cell = (f"{_direction_route(d)} {d.depart:%H:%M}→{d.arrive:%H:%M}, "
            f"{_fmt_hm(d.duration_minutes)}, {d.main_carrier_name}")
    if d.transfer_airports:
        cell += f", пересадка: {','.join(d.transfer_airports)}"
    return cell


def _baggage_cell(t: Ticket) -> str:
    """"да (N кг)" когда вес известен, "да" когда багаж есть но вес неизвестен,
    иначе "нет". Вес — "оптимистичный" (см. Ticket.baggage_weight_kg): честен,
    пока has_baggage=True, даже если багаж есть не на всех плечах."""
    if not t.has_baggage:
        return "нет"
    if t.baggage_weight_kg is not None:
        return f"да ({t.baggage_weight_kg} кг)"
    return "да"


def _link_cell(t: Ticket) -> str:
    return f"[билет]({t.deep_link})" if t.deep_link else "—"


def _alt_link(t: Ticket) -> str:
    """Ссылка на альтернативу (если есть deep_link)."""
    return f" [↗]({t.deep_link})" if t.deep_link else ""


def _describe_alternative(alt: Ticket, best: Ticket, labels: list[str]) -> str:
    """Только отличающиеся от лучшего билета плечи, кратко: метка, перевозчик
    (если другой), время вылета, маршрут (если другие аэропорты)."""
    parts = []
    for label, a, b in zip(labels, alt.directions, best.directions):
        same = (a.depart == b.depart and _direction_route(a) == _direction_route(b)
                and a.main_carrier_name == b.main_carrier_name)
        if same:
            continue
        bits = [label.lower()]
        if a.main_carrier_name != b.main_carrier_name:
            bits.append(a.main_carrier_name)
        bits.append(f"{a.depart:%H:%M}")
        if _direction_route(a) != _direction_route(b):
            bits.append(_direction_route(a))
        parts.append(" ".join(bits))
    return ", ".join(parts) if parts else "другой тариф"


def _render_comment(group: ComboGroup, labels: list[str]) -> str:
    """Форматирует комментарий: список альтернатив с ценами и отличиями от best."""
    best = group.best
    items = [
        f"та же цена: {_describe_alternative(t, best, labels)}{_alt_link(t)}"
        for t in group.equal_alternatives
    ]
    items += [
        f"+{_spaced(t.price_rub - best.price_rub)} ₽: "
        f"{_describe_alternative(t, best, labels)}{_alt_link(t)}"
        for t in group.pricier_alternatives
    ]
    return "; ".join(items) if items else "—"


def _render_row(idx: int, g: ComboGroup, labels: list[str]) -> str:
    t = g.best
    legs = " | ".join(_leg_cell(d) for d in t.directions)
    return (f"| {idx} | {_dates_cell(g.dates)} | {legs} | {_fmt_rub(t.price_rub)} | "
            f"{_baggage_cell(t)} | {_link_cell(t)} | {_render_comment(g, labels)} |")


def _delta_header(cheapest_rub: int, previous_offers: list[dict]) -> list[str]:
    prev_best = min(o["price_rub"] for o in previous_offers)
    delta = cheapest_rub - prev_best
    sign = "−" if delta < 0 else "+"
    return [
        f"Лучшая цена: {_fmt_rub(cheapest_rub)} "
        f"(было {_fmt_rub(prev_best)}, {sign}{_spaced(abs(delta))})",
        "",
    ]


def render_markdown(tickets: list[Ticket], top_n: int | None = None,
                    previous_offers: list[dict] | None = None,
                    direction_labels: list[str] | None = None) -> str:
    if not tickets:
        return "# Результаты поиска\n\nПодходящих вариантов не найдено.\n"

    groups = group_tickets_by_combo(tickets)
    if top_n is not None:
        groups = groups[:top_n]
    labels = direction_labels or _fallback_labels(len(groups[0].best.directions))

    lines = ["# Результаты поиска Aviasales", ""]
    if previous_offers:
        lines += _delta_header(groups[0].best.price_rub, previous_offers)
    lines.append("| # | Даты | " + " | ".join(labels)
                 + " | Итого | Багаж | Ссылка | Комментарий |")
    lines.append("|" + "---|" * (len(labels) + 6))
    for i, g in enumerate(groups, start=1):
        lines.append(_render_row(i, g, labels))
    lines.append("")
    return "\n".join(lines)
