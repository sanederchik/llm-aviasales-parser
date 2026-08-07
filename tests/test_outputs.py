import datetime as dt
import json
from pathlib import Path

from aviasales_search.csv_report import BOM
from aviasales_search.outputs import LiveOutputsWriter, sibling_output_paths
from aviasales_search.trip_model import DirectionResult, FlightLeg, Ticket

# --------------------------- builders ---------------------------


def _ts(d: dt.datetime) -> int:
    """Naive-даты тестов трактуем как UTC, чтобы разницы ts совпадали с
    naive-дельтами независимо от таймзоны машины."""
    return int(d.replace(tzinfo=dt.timezone.utc).timestamp())


def _leg(origin, destination, dep, arr, carrier="SU", flight="SU1", carrier_name="Аэрофлот"):
    return FlightLeg(
        origin=origin, destination=destination, departure=dep, arrival=arr,
        carrier=carrier, flight_number=flight,
        departure_ts=_ts(dep), arrival_ts=_ts(arr), carrier_name=carrier_name,
    )


def _direct_direction(origin, destination, date_iso, dep_hour=10, duration_hours=3):
    dep = dt.datetime.fromisoformat(f"{date_iso}T{dep_hour:02d}:00:00")
    arr = dep + dt.timedelta(hours=duration_hours)
    return DirectionResult(legs=[_leg(origin, destination, dep, arr)])


def _roundtrip_ticket(price, date_out="2026-09-15"):
    return Ticket(price_rub=price, has_baggage=True, deep_link="https://aviasales.ru/x",
                  directions=[
                      _direct_direction("MOW", "DPS", date_out),
                      _direct_direction("DPS", "MOW", "2026-12-15"),
                  ])


TRIP = {"directions": [{"from": "MOW", "to": "DPS"}, {"from": "DPS", "to": "MOW"}]}
NOW = dt.datetime(2026, 8, 5, 12, 0)


def _writer(tmp_path, **kwargs):
    md = tmp_path / "reports" / "trip.md"
    json_path, csv_path = sibling_output_paths(md)
    return LiveOutputsWriter(md, json_path, csv_path, trip=TRIP,
                             generated_at=NOW, **kwargs), md, json_path, csv_path


# --------------------------- sibling_output_paths ---------------------------


def test_sibling_output_paths():
    json_path, csv_path = sibling_output_paths(Path("reports/mow-dps-2026-09.md"))
    assert json_path == Path("reports/json/mow-dps-2026-09.json")
    assert csv_path == Path("reports/csv/mow-dps-2026-09.csv")


# --------------------------- LiveOutputsWriter ---------------------------


def test_update_writes_all_three_files(tmp_path):
    writer, md, json_path, csv_path = _writer(tmp_path)
    writer.update([_roundtrip_ticket(1000)])
    assert "Результаты поиска" in md.read_text()
    data = json.loads(json_path.read_text())
    assert data["schema_version"] == 1
    assert data["offers"][0]["price_rub"] == 1000
    csv_text = csv_path.read_text()
    assert csv_text.startswith(BOM)
    assert "mow_dps_длит_мин" in csv_text
    assert writer.last_markdown == md.read_text()


def test_update_skips_rewrite_when_content_unchanged(tmp_path):
    writer, md, json_path, csv_path = _writer(tmp_path)
    tickets = [_roundtrip_ticket(1000)]
    writer.update(tickets)
    # Портим файлы снаружи: повторный update с тем же контентом НЕ должен
    # их трогать (писатель сравнивает с последним отрендеренным).
    for p in (md, json_path, csv_path):
        p.write_text("tampered", encoding="utf-8")
    writer.update(tickets)
    for p in (md, json_path, csv_path):
        assert p.read_text() == "tampered"


def test_update_rewrites_when_tickets_change(tmp_path):
    writer, md, json_path, csv_path = _writer(tmp_path)
    writer.update([_roundtrip_ticket(1000)])
    writer.update([_roundtrip_ticket(1000), _roundtrip_ticket(500)])
    data = json.loads(json_path.read_text())
    assert [o["price_rub"] for o in data["offers"]] == [500, 1000]
    assert "500" in csv_path.read_text()


def test_update_leaves_no_temp_files(tmp_path):
    writer, md, json_path, csv_path = _writer(tmp_path)
    writer.update([_roundtrip_ticket(1000)])
    leftovers = [p for p in tmp_path.rglob("*.tmp")]
    assert leftovers == []


def test_csv_includes_all_tickets_not_only_best_per_combo(tmp_path):
    # Два билета на ОДНУ комбинацию дат: в MD второй уйдёт в «Комментарий»,
    # а в CSV и JSON оба должны быть отдельными строками/офферами.
    writer, md, json_path, csv_path = _writer(tmp_path)
    writer.update([_roundtrip_ticket(1000), _roundtrip_ticket(1200)])
    data = json.loads(json_path.read_text())
    assert len(data["offers"]) == 2
    csv_rows = csv_path.read_text().strip().splitlines()
    assert len(csv_rows) == 1 + 2  # заголовок + два билета


def test_top_n_limits_markdown_but_not_json_csv(tmp_path):
    writer, md, json_path, csv_path = _writer(tmp_path, top_n=1)
    writer.update([_roundtrip_ticket(1000), _roundtrip_ticket(500, date_out="2026-09-16")])
    md_text = md.read_text()
    assert md_text.count("| 1 |") == 1 and "| 2 |" not in md_text
    assert len(json.loads(json_path.read_text())["offers"]) == 2
    assert len(csv_path.read_text().strip().splitlines()) == 3
