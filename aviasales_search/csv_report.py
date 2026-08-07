"""CSV-отчёт: широкая таблица «одна строка = один билет на всю поездку», по
каждому плечу свой набор столбцов с префиксом маршрута — чтобы фильтровать в
Excel/Sheets по параметрам отдельных плеч. Числовые столбцы (цена, минуты,
пересадки) — целые числа; даты/время — ISO. Кодировка UTF-8 с BOM (иначе
Excel ломает кириллицу), разделитель — запятая."""

from __future__ import annotations

import csv
import io

from .trip_model import DirectionResult, Ticket

# U+FEFF: именованная константа вместо литерала — невидимый символ в
# исходнике невозможно отличить от его отсутствия.
BOM = chr(0xFEFF)

_DIRECTION_COLUMNS = ("дата", "вылет", "прилет", "длит_мин",
                      "авиакомпания", "пересадки", "аэропорты_пересадок")


def route_pairs_from_trip(trip: dict) -> list[tuple[str, str]]:
    """Пары (откуда, куда) плеч из конфига поездки (сырого dict)."""
    return [(d["from"], d["to"]) for d in trip["directions"]]


def csv_prefixes(route_pairs: list[tuple[str, str]]) -> list[str]:
    """`[("MOW","TAS"), …]` → `["mow_tas", …]`; повторяющийся маршрут получает
    индекс со второго вхождения: mow_tas, mow_tas_2."""
    counts: dict[str, int] = {}
    out = []
    for origin, dest in route_pairs:
        base = f"{origin.lower()}_{dest.lower()}"
        counts[base] = counts.get(base, 0) + 1
        out.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return out


def _direction_cells(d: DirectionResult) -> list:
    return [
        d.depart.date().isoformat(),
        f"{d.depart:%H:%M}",
        # Прилёт — полной датой-временем: бывает на следующий день.
        d.arrive.isoformat(timespec="minutes"),
        d.duration_minutes,
        d.main_carrier_name,
        d.transfers,
        ",".join(d.transfer_airports),
    ]


def render_csv(tickets: list[Ticket],
               route_pairs: list[tuple[str, str]]) -> str:
    header = ["цена_руб", "багаж", "багаж_кг"]
    for prefix in csv_prefixes(route_pairs):
        header += [f"{prefix}_{col}" for col in _DIRECTION_COLUMNS]
    header.append("ссылка")

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for t in sorted(tickets, key=lambda t: t.price_rub):
        # baggage_weight_kg — "оптимистичный" вес (см. Ticket): пусто, если
        # неизвестен/багажа нет; ошибка вида 10 кг вместо 20 видна глазами.
        row: list = [
            t.price_rub,
            "да" if t.has_baggage else "нет",
            t.baggage_weight_kg if t.baggage_weight_kg is not None else "",
        ]
        for d in t.directions:
            row += _direction_cells(d)
        row.append(t.deep_link)
        writer.writerow(row)
    return BOM + buf.getvalue()
