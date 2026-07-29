import datetime as dt

from aviasales_search.report import (
    ComboGroup,
    LiveReportWriter,
    combo_dates,
    group_tickets_by_combo,
    itinerary_to_offer_dict,
    offers_sorted_desc,
    render_markdown,
)
from aviasales_search.trip_model import DirectionResult, FlightLeg, Ticket

# --------------------------- builders ---------------------------


def _ts(d: dt.datetime) -> int:
    """Treat naive test datetimes as UTC so timestamp diffs equal naive deltas
    exactly, independent of the machine's local timezone/DST."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="SU", flight="SU1"):
    return FlightLeg(
        origin=origin, destination=destination, departure=dep, arrival=arr,
        carrier=carrier, flight_number=flight,
        departure_ts=_ts(dep), arrival_ts=_ts(arr),
    )


def _direct_direction(origin, destination, date_iso, dep_hour=10, duration_hours=3,
                      carrier="SU"):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr = dep + dt.timedelta(hours=duration_hours)
    return DirectionResult(legs=[_leg(origin, destination, dep, arr, carrier=carrier)])


def _transfer_direction(origin, hub, destination, date_iso, gap_minutes=120,
                        leg_hours=3, dep_hour=10, carrier="SU"):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr1 = dep + dt.timedelta(hours=leg_hours)
    dep2 = arr1 + dt.timedelta(minutes=gap_minutes)
    arr2 = dep2 + dt.timedelta(hours=leg_hours)
    return DirectionResult(legs=[
        _leg(origin, hub, dep, arr1, carrier=carrier),
        _leg(hub, destination, dep2, arr2, carrier=carrier),
    ])


def _ticket(directions, price=1000, has_baggage=True, deep_link="https://aviasales.ru/x"):
    return Ticket(price_rub=price, directions=directions, has_baggage=has_baggage,
                 deep_link=deep_link)


def _roundtrip_ticket(price, date_out="2026-09-15", date_back="2026-12-15"):
    return _ticket([
        _direct_direction("MOW", "DPS", date_out),
        _direct_direction("DPS", "MOW", date_back),
    ], price=price)


# --------------------------- itinerary_to_offer_dict ---------------------------


def test_itinerary_to_offer_dict_has_price_and_route():
    t = _roundtrip_ticket(160423)
    d = itinerary_to_offer_dict(t)
    assert d["price_rub"] == 160423
    assert d["route"] == "MOW→DPS ⇄ DPS→MOW"


# --------------------------- offers_sorted_desc ---------------------------


def test_offers_sorted_desc():
    offers = offers_sorted_desc([
        _roundtrip_ticket(100), _roundtrip_ticket(300), _roundtrip_ticket(200),
    ])
    assert [o["price_rub"] for o in offers] == [300, 200, 100]


def test_offers_sorted_desc_empty():
    assert offers_sorted_desc([]) == []


# --------------------------- render_markdown ---------------------------


def test_render_markdown_empty_list():
    md = render_markdown([])
    assert "не найдено" in md.lower()


def test_render_markdown_formats_price_with_space_separator():
    ticket = _roundtrip_ticket(160423)
    md = render_markdown([ticket])
    assert "160 423" in md


def test_render_markdown_one_row_per_combo_sorted_by_price():
    md = render_markdown([
        _roundtrip_ticket(300, date_out="2026-09-16"),
        _roundtrip_ticket(100, date_out="2026-09-15"),
        _roundtrip_ticket(150, date_out="2026-09-15"),  # та же комбинация, дороже
    ])
    assert md.count("| 1 |") == 1 and md.count("| 2 |") == 1
    assert "15.09 → 15.12" in md and "16.09 → 15.12" in md
    assert md.index("15.09") < md.index("16.09")        # дешёвая комбинация выше
    assert "100 ₽" in md and "300 ₽" in md


def test_render_markdown_top_n_limits_combos_not_tickets():
    md = render_markdown([
        _roundtrip_ticket(100, date_out="2026-09-15"),
        _roundtrip_ticket(200, date_out="2026-09-16"),
        _roundtrip_ticket(300, date_out="2026-09-17"),
    ], top_n=2)
    assert "15.09" in md and "16.09" in md
    assert "17.09" not in md


def test_render_markdown_flat_table_columns_roundtrip():
    md = render_markdown([_roundtrip_ticket(100000)])
    header = next(line for line in md.splitlines() if line.startswith("| #"))
    assert header == "| # | Даты | Туда | Обратно | Итого | Ссылка | Комментарий |"


def test_render_markdown_custom_direction_labels():
    md = render_markdown([_roundtrip_ticket(100000)],
                         direction_labels=["MOW→DPS", "DPS→MOW"])
    assert "| MOW→DPS | DPS→MOW |" in "".join(md.splitlines())


def test_render_markdown_leg_cell_contents():
    ticket = _ticket([
        _transfer_direction("SVO", "CAN", "DPS", "2026-09-15", dep_hour=21,
                            gap_minutes=145, leg_hours=10, carrier="CZ"),
        _direct_direction("DPS", "MOW", "2026-12-15"),
    ], price=160423)
    md = render_markdown([ticket])
    assert "SVO→DPS 21:00→19:25" in md          # аэропорты и времена
    assert "22ч25м" in md                        # tz-корректная длительность
    assert "пересадка: CAN" in md
    assert "CZ" in md


def test_render_markdown_link_column_and_dash_fallback():
    with_link = _roundtrip_ticket(100, date_out="2026-09-15")
    md = render_markdown([with_link])
    assert "[билет](https://aviasales.ru/x)" in md

    no_link = _ticket([
        _direct_direction("MOW", "DPS", "2026-09-16"),
        _direct_direction("DPS", "MOW", "2026-12-15"),
    ], price=200, deep_link="")
    md2 = render_markdown([no_link])
    row = next(line for line in md2.splitlines() if "| 1 |" in line)
    assert "| — |" in row                        # нет ссылки — честный прочерк


def test_render_markdown_price_delta_shows_cheaper():
    md = render_markdown([_roundtrip_ticket(150)], previous_offers=[{"price_rub": 200}])
    assert "было 200" in md or "−50" in md or "-50" in md


def test_render_markdown_price_delta_shows_more_expensive():
    md = render_markdown([_roundtrip_ticket(250)], previous_offers=[{"price_rub": 200}])
    assert "было 200" in md
    assert "+50" in md


def test_render_markdown_no_delta_section_without_previous_offers():
    md = render_markdown([_roundtrip_ticket(150)])
    assert "было" not in md


# --------------------------- group_tickets_by_combo ---------------------------


def test_combo_dates_is_tuple_of_departure_dates():
    t = _roundtrip_ticket(100, date_out="2026-09-15", date_back="2026-12-15")
    assert combo_dates(t) == (dt.date(2026, 9, 15), dt.date(2026, 12, 15))


def test_group_picks_cheapest_per_combo_and_sorts_groups_by_price():
    groups = group_tickets_by_combo([
        _roundtrip_ticket(300, date_out="2026-09-15"),  # combo A, дороже
        _roundtrip_ticket(100, date_out="2026-09-15"),  # combo A, дешевле
        _roundtrip_ticket(200, date_out="2026-09-16"),  # combo B
    ])
    assert len(groups) == 2
    assert groups[0].best.price_rub == 100          # combo A первым (дешевле)
    assert groups[1].best.price_rub == 200
    assert groups[0].dates[0] == dt.date(2026, 9, 15)


def test_group_price_tie_breaks_by_dates():
    groups = group_tickets_by_combo([
        _roundtrip_ticket(100, date_out="2026-09-16"),
        _roundtrip_ticket(100, date_out="2026-09-15"),
    ])
    assert [g.dates[0].day for g in groups] == [15, 16]


def test_equal_alternatives_excludes_best_and_keeps_same_price_only():
    g = group_tickets_by_combo([
        _roundtrip_ticket(100), _roundtrip_ticket(100), _roundtrip_ticket(150),
    ])[0]
    assert [t.price_rub for t in g.equal_alternatives] == [100]


def test_pricier_alternatives_capped_at_three():
    g = group_tickets_by_combo([
        _roundtrip_ticket(100), _roundtrip_ticket(110), _roundtrip_ticket(120),
        _roundtrip_ticket(130), _roundtrip_ticket(140),
    ])[0]
    assert [t.price_rub for t in g.pricier_alternatives] == [110, 120, 130]


# --------------------------- комментарий (альтернативы) ---------------------------


def _rt(price, out_hour=10, out_carrier="SU", deep_link="https://aviasales.ru/x"):
    return _ticket([
        _direct_direction("MOW", "DPS", "2026-09-15", dep_hour=out_hour,
                          carrier=out_carrier),
        _direct_direction("DPS", "MOW", "2026-12-15"),
    ], price=price, deep_link=deep_link)


def test_comment_dash_when_no_alternatives():
    md = render_markdown([_rt(100)])
    row = next(line for line in md.splitlines() if "| 1 |" in line)
    assert row.rstrip("| ").endswith("—")


def test_comment_equal_price_alternative_with_link():
    md = render_markdown([
        _rt(100, out_hour=6),
        _rt(100, out_hour=23, deep_link="https://aviasales.ru/alt"),
    ])
    assert "та же цена:" in md
    assert "23:00" in md                          # отличие — время вылета
    assert "[↗](https://aviasales.ru/alt)" in md


def test_comment_pricier_alternatives_show_delta_and_cap():
    md = render_markdown([
        _rt(100), _rt(120, out_hour=11), _rt(130, out_hour=12),
        _rt(140, out_hour=13), _rt(150, out_hour=14),
    ])
    assert "+20 ₽:" in md and "+30 ₽:" in md and "+40 ₽:" in md
    assert "+50" not in md                        # лимит 3 более дорогих


def test_comment_mentions_carrier_when_it_differs():
    md = render_markdown([
        _rt(100, out_carrier="SU"),
        _rt(100, out_hour=10, out_carrier="S7"),  # то же время, другой перевозчик
    ])
    assert "S7" in md.split("Комментарий")[-1]


def test_comment_identical_flights_different_fare():
    md = render_markdown([_rt(100), _rt(180)])    # одинаковые рейсы, цены разные
    assert "другой тариф" in md


# --------------------------- LiveReportWriter ---------------------------


def _priced_ticket(price, date_iso="2026-09-15", signature=None):
    sig = signature if signature is not None else f"sig-{price}"
    return Ticket(
        price_rub=price,
        directions=[_direct_direction("MOW", "DPS", date_iso)],
        has_baggage=True,
        deep_link=f"https://www.aviasales.ru/search/x?t=SU_{sig}_{price}",
        signature=sig,
    )


def test_live_report_writer_writes_report_on_first_update(tmp_path):
    out = tmp_path / "report.md"
    writer = LiveReportWriter(out, top_n=10)
    writer.update([_priced_ticket(1000)])
    text = out.read_text()
    assert "Результаты поиска" in text
    assert "1 000 ₽" in text


def test_live_report_writer_skips_rewrite_when_top_unchanged(tmp_path):
    out = tmp_path / "report.md"
    writer = LiveReportWriter(out, top_n=10)
    tickets = [_priced_ticket(1000), _priced_ticket(2000)]
    writer.update(tickets)
    out.unlink()  # если топ не изменился, файл не должен появиться снова
    writer.update(list(tickets))
    assert not out.exists()


def test_live_report_writer_rewrites_when_top_changes(tmp_path):
    out = tmp_path / "report.md"
    writer = LiveReportWriter(out, top_n=10)
    writer.update([_priced_ticket(2000)])
    writer.update([_priced_ticket(1000), _priced_ticket(2000)])
    assert "1 000 ₽" in out.read_text()


def test_live_report_writer_ignores_combos_beyond_top_n(tmp_path):
    out = tmp_path / "report.md"
    writer = LiveReportWriter(out, top_n=2)
    tickets = [_priced_ticket(1000, date_iso="2026-09-15"),
               _priced_ticket(2000, date_iso="2026-09-16")]
    writer.update(tickets)
    out.unlink()
    writer.update(tickets + [_priced_ticket(3000, date_iso="2026-09-17")])
    assert not out.exists()                      # топ-2 комбинаций не изменился


def test_live_report_writer_leaves_no_temp_files(tmp_path):
    out = tmp_path / "report.md"
    writer = LiveReportWriter(out, top_n=10)
    writer.update([_priced_ticket(1000)])
    writer.update([_priced_ticket(500), _priced_ticket(1000)])
    assert [p.name for p in tmp_path.iterdir()] == ["report.md"]
