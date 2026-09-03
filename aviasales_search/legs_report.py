"""Рендер промежуточного отчёта (Фаза 1) из JSON-первоисточника: секция-таблица
`дата → мин. цена` на каждое плечо (стиль legs-min-prices), затем ранкинг комбо
по возрастанию суммы (стиль combo-отчёта). Минимум каждого плеча выделен."""
from __future__ import annotations

from .report import _spaced


def _ddmm(date_iso: str) -> str:
    _, month, day = date_iso.split("-")
    return f"{day}.{month}"


def _leg_section(leg: dict) -> str:
    dates = leg["dates"]
    prices = [d["min_price_rub"] for d in dates if d["min_price_rub"] is not None]
    min_price = min(prices) if prices else None
    header = f"## {leg['route']}"
    cols = " | ".join(_ddmm(d["date"]) for d in dates)
    sep = "|".join(["---"] * (len(dates) + 1))
    cells = []
    for d in dates:
        p = d["min_price_rub"]
        if p is None:
            cells.append("—")  # дата свипнута, вариантов под фильтры нет
        elif p == min_price:
            cells.append(f"**{_spaced(p)}**")  # минимум плеча
        else:
            cells.append(_spaced(p))
    row = f"| {leg['route']} | " + " | ".join(cells) + " |"
    return f"{header}\n\n| Плечо | {cols} |\n|{sep}|\n{row}\n"


def _ranking_section(ranking: list[dict], legs: list[dict]) -> str:
    routes = [leg["route"] for leg in legs]
    stay_cols = [f"{legs[i]['destination']}, дн" for i in range(len(legs) - 1)]
    head = "| # | " + " | ".join(routes) + " | " + " | ".join(stay_cols) + " | Итого |"
    sep = "|".join(["---"] * (1 + len(routes) + len(stay_cols) + 1))
    lines = [head, f"|{sep}|"]
    for idx, rc in enumerate(ranking, start=1):
        leg_cells = [
            f"{_ddmm(rc['combo'][i])} ({_spaced(rc['per_leg_min_rub'][i])})"
            for i in range(len(routes))
        ]
        stay_cells = [str(s) for s in rc["stay_days"]]
        total = f"**{_spaced(rc['total_rub'])}**"
        lines.append(
            f"| {idx} | " + " | ".join(leg_cells) + " | " + " | ".join(stay_cells)
            + f" | {total} |"
        )
    return "\n".join(lines)


def render_legs_markdown(data: dict) -> str:
    legs = data["legs"]
    parts = [
        "# Промежуточный отчёт: минимумы по плечам и ранкинг комбинаций",
        "",
        f"Снято: {data['generated_at'][:10]}. «—» = на эту дату вариантов, "
        "проходящих фильтры, не нашлось.",
        "",
    ]
    for leg in legs:
        parts.append(_leg_section(leg))
    parts.append("## Ранкинг комбинаций по возрастанию суммы\n")
    parts.append(
        "Сумма = минимальные цены каждого плеча по отдельности — это **оценка**; "
        "реальная цена связки берётся в Фазе 2 и может отличаться.\n"
    )
    parts.append(_ranking_section(data["ranking"], legs))
    parts.append("")
    return "\n".join(parts)
