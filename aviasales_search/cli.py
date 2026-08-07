"""Точка входа CLI (`aviasales-search`): связывает конфиг → auth → клиент →
планнер → отчёт/снапшот. Побочные эффекты (файлы, время, сеть) остаются на
границе (`run`/`main`); сама логика поиска и фильтрации живёт в других
модулях (см. `planner.py`, `search_client.py`)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path
from typing import Optional

from .cache import ProbeCache, _run_dirname, latest_previous_offers, write_snapshot
from .curl_auth import ExpiredCurlError, parse_curl_auth
from .outputs import LiveOutputsWriter, sibling_output_paths
from .planner import Planner
from .progress import ProgressLog
from .report import direction_labels_for, offers_sorted_desc
from .results_json import ResultsJsonError, tickets_from_json
from .search_client import SearchClient, Transport, default_transport
from .trip_model import ConfigError, Itinerary, parse_config


def default_report_path(config: Itinerary) -> Path:
    """reports/<цепочка-пунктов>-<YYYY-MM>.md в текущей директории поиска."""
    chain = [config.directions[0].origin] + [d.destination for d in config.directions]
    if len(chain) > 1 and chain[-1] == chain[0]:
        chain = chain[:-1]
    slug = "-".join(p.lower() for p in chain)
    month = config.directions[0].date_window.earliest.strftime("%Y-%m")
    return Path("reports") / f"{slug}-{month}.md"


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aviasales-search",
        description="Поиск лучших авиаперелётов на aviasales.ru",
    )
    p.add_argument("--config", help="JSON-конфиг поездки (обязателен без --render-from)")
    p.add_argument("--curl", help="Файл с Copy as cURL (обязателен без --render-from)")
    p.add_argument("--out", help="Куда записать Markdown-отчёт "
                                  "(по умолчанию reports/<маршрут>-<год-месяц>.md)")
    p.add_argument("--top", type=int, default=None,
                   help="Сколько комбинаций дат показать (по умолчанию все)")
    p.add_argument("--refresh", action="store_true", help="Игнорировать кэш")
    p.add_argument("--max-requests", type=int, help="Переопределить бюджет запросов")
    p.add_argument("--cache-dir", default="cache", help="Папка кэша (по умолчанию ./cache)")
    p.add_argument("--render-from", metavar="RESULTS_JSON",
                   help="Пересобрать MD и CSV из готового JSON результатов "
                        "без нового поиска (--config/--curl не нужны)")
    return p


def run_render_from(args) -> int:
    """Пересборка отчётов из файла результатов (обычно reports/json/<…>.json): сеть и cURL не нужны.
    generated_at сохраняется исходный — файл не перештамповывается."""
    try:
        data = json.loads(Path(args.render_from).read_text(encoding="utf-8"))
        tickets = tickets_from_json(data)
        config = parse_config(data["trip"])
        generated_at = dt.datetime.fromisoformat(data["generated_at"])
    except (OSError, json.JSONDecodeError, ResultsJsonError, ConfigError,
            KeyError, ValueError) as exc:
        print(f"Ошибка файла результатов: {exc}", file=sys.stderr)
        return 1

    out_path = Path(args.out) if args.out else default_report_path(config)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    json_path, csv_path = sibling_output_paths(out_path)
    writer = LiveOutputsWriter(out_path, json_path, csv_path, trip=data["trip"],
                               generated_at=generated_at, top_n=args.top,
                               direction_labels=direction_labels_for(config))
    writer.update(tickets)
    print(writer.last_markdown)
    return 0


def run(
    args,
    now: dt.datetime,
    transport: Optional[Transport] = None,
    sleep=time.sleep,
) -> int:
    if args.render_from:
        return run_render_from(args)

    if not args.config or not args.curl:
        print("Нужны --config и --curl (либо --render-from <results.json>)",
              file=sys.stderr)
        return 1

    if transport is None:
        # Fail loud: `run` never silently constructs a live-network transport.
        # Only `main()` decides to use the real network via `default_transport()`.
        raise ValueError("transport is required; main() supplies default_transport()")

    try:
        raw_config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        config = parse_config(raw_config)
    except (ConfigError, json.JSONDecodeError, OSError) as exc:
        print(f"Ошибка конфига: {exc}", file=sys.stderr)
        return 1

    if args.max_requests is not None:
        config.search_budget.max_requests = args.max_requests
        raw_config.setdefault("search_budget", {})["max_requests"] = args.max_requests

    out_path = Path(args.out) if args.out else default_report_path(config)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    labels = direction_labels_for(config)

    try:
        auth = parse_curl_auth(Path(args.curl).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Ошибка cURL-файла: {exc}", file=sys.stderr)
        return 2

    cache_dir = Path(args.cache_dir)
    probe_cache = ProbeCache(cache_dir / "probes.jsonl")
    client = SearchClient(auth=auth, transport=transport, sleep=sleep,
                          delay_min=config.search_budget.delay_min_seconds,
                          delay_max=config.search_budget.delay_max_seconds)

    previous = latest_previous_offers(cache_dir, before_ts=now, trip=raw_config)

    # Живые результаты: MD + JSON + CSV обновляются по ходу прогона при каждом
    # изменении — даже при обрыве (бан, Ctrl+C) в них остаётся лучшее из
    # найденного. JSON — первоисточник: MD и CSV рендерятся из него.
    json_path, csv_path = sibling_output_paths(out_path)
    writer = LiveOutputsWriter(out_path, json_path, csv_path, trip=raw_config,
                               generated_at=now, top_n=args.top,
                               previous_offers=previous, direction_labels=labels)

    # Лог прогресса живёт в папке прогона (тот же timestamp использует
    # write_snapshot ниже — снапшот и лог лягут рядом) и дублируется в stderr.
    run_dir = cache_dir / "runs" / _run_dirname(now)
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "search.log").open("w", encoding="utf-8") as log_file:
        planner = Planner(config, client, probe_cache, now=now, refresh=args.refresh,
                          progress=ProgressLog([sys.stderr, log_file]),
                          live_report=writer.update)
        try:
            tickets = planner.plan()
        except ExpiredCurlError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    writer.update(tickets)
    report_md = writer.last_markdown
    write_snapshot(cache_dir, now, raw_config, offers_sorted_desc(tickets), report_md)
    print(report_md)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    # В режиме --render-from сеть не нужна: default_transport() импортирует
    # curl_cffi, которого может не быть без extra `live` — не строим транспорт зря.
    transport = None if args.render_from else default_transport()
    return run(args, now=dt.datetime.now(), transport=transport)


if __name__ == "__main__":
    raise SystemExit(main())
