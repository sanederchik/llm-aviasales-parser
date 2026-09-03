from aviasales_search.legs_report import render_legs_markdown


def _data():
    return {
        "schema_version": 1,
        "generated_at": "2026-08-08T12:00:00",
        "trip": {},
        "legs": [
            {"route": "MOW→ALA", "origin": "MOW", "destination": "ALA", "dates": [
                {"date": "2026-09-10", "min_price_rub": 50972, "carrier": "TK",
                 "carrier_name": "Turkish", "transfers": 0, "transfer_airports": [],
                 "deep_link": "l1"},
                {"date": "2026-09-11", "min_price_rub": 44492, "carrier": "TK",
                 "carrier_name": "Turkish", "transfers": 0, "transfer_airports": [],
                 "deep_link": "l2"},
            ]},
            {"route": "ALA→TAS", "origin": "ALA", "destination": "TAS", "dates": [
                {"date": "2026-09-16", "min_price_rub": 20000, "carrier": "KC",
                 "carrier_name": "Air Astana", "transfers": 0, "transfer_airports": [],
                 "deep_link": "l3"},
            ]},
        ],
        "ranking": [
            {"combo": ["2026-09-11", "2026-09-16"], "per_leg_min_rub": [44492, 20000],
             "stay_days": [5], "total_rub": 64492},
            {"combo": ["2026-09-10", "2026-09-16"], "per_leg_min_rub": [50972, 20000],
             "stay_days": [6], "total_rub": 70972},
        ],
    }


def test_render_legs_markdown_has_leg_tables_and_ranking():
    md = render_legs_markdown(_data())
    # секция на каждое плечо с таблицей дата→цена
    assert "## MOW→ALA" in md
    assert "## ALA→TAS" in md
    # минимум плеча выделен жирным
    assert "**44 492**" in md
    # ранкинг в конце, по возрастанию суммы, первая строка — минимальная
    ranking_idx = md.index("Ранкинг")
    assert md.index("64 492", ranking_idx) < md.index("70 972", ranking_idx)
    # оговорка про оценку
    assert "оценка" in md.lower()


def test_render_legs_markdown_renders_dash_for_null_dates():
    data = {
        "schema_version": 1, "generated_at": "2026-08-08T12:00:00", "trip": {},
        "legs": [{"route": "MOW→ALA", "origin": "MOW", "destination": "ALA", "dates": [
            {"date": "2026-09-10", "min_price_rub": 50000, "carrier": "TK",
             "carrier_name": "T", "transfers": 0, "transfer_airports": [], "deep_link": "l"},
            {"date": "2026-09-11", "min_price_rub": None},
        ]}],
        "ranking": [],
    }
    md = render_legs_markdown(data)
    # пустая дата -> «—», минимум по непустым выделен
    assert "| **50 000** | — |" in md
