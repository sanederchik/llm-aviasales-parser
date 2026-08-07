"""Запись трёх файлов результатов (MD / JSON / CSV) — и «живьём» по ходу
прогона, и финально. JSON — первоисточник: MD и CSV рендерятся из билетов,
восстановленных из только что сериализованного JSON (гарантия его
самодостаточности). Каждый файл перезаписывается только когда содержимое
реально изменилось; замена атомарная (tmp + os.replace), чтобы читатель не
увидел полузаписанный файл."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

from .csv_report import render_csv, route_pairs_from_trip
from .report import render_markdown
from .results_json import tickets_from_json, tickets_to_json
from .trip_model import Ticket


def sibling_output_paths(md_path: Path) -> tuple[Path, Path]:
    """reports/x.md → (reports/json/x.json, reports/csv/x.csv)."""
    return (md_path.parent / "json" / (md_path.stem + ".json"),
            md_path.parent / "csv" / (md_path.stem + ".csv"))


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


class LiveOutputsWriter:
    def __init__(self, md_path, json_path, csv_path, trip: dict,
                 generated_at: dt.datetime, top_n: int | None = None,
                 previous_offers: list[dict] | None = None,
                 direction_labels: list[str] | None = None):
        self.md_path = Path(md_path)
        self.json_path = Path(json_path)
        self.csv_path = Path(csv_path)
        self.trip = trip
        self.generated_at = generated_at
        self.top_n = top_n
        self.previous_offers = previous_offers
        self.direction_labels = direction_labels
        self.route_pairs = route_pairs_from_trip(trip)
        self.last_markdown: str | None = None
        self._last_written: dict[Path, str] = {}

    def update(self, tickets: list[Ticket]) -> None:
        data = tickets_to_json(tickets, self.trip, self.generated_at)
        restored = tickets_from_json(data)
        self.last_markdown = render_markdown(
            restored, top_n=self.top_n, previous_offers=self.previous_offers,
            direction_labels=self.direction_labels)
        renders = {
            self.json_path: json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            self.md_path: self.last_markdown,
            self.csv_path: render_csv(restored, self.route_pairs),
        }
        for path, content in renders.items():
            if self._last_written.get(path) != content:
                self._last_written[path] = content
                _write_atomic(path, content)
