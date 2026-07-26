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

from .cache import ProbeCache, latest_previous_offers, write_snapshot
from .curl_auth import ExpiredCurlError, parse_curl_auth
from .planner import Planner
from .report import offers_sorted_desc, render_markdown
from .search_client import SearchClient, Transport, default_transport
from .trip_model import ConfigError, parse_config


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aviasales-search",
        description="Поиск лучших авиаперелётов на aviasales.ru",
    )
    p.add_argument("--config", required=True, help="JSON-конфиг поездки")
    p.add_argument("--curl", required=True, help="Файл с Copy as cURL (авторизация)")
    p.add_argument("--out", help="Куда записать Markdown-отчёт")
    p.add_argument("--top", type=int, default=10, help="Сколько вариантов показать")
    p.add_argument("--refresh", action="store_true", help="Игнорировать кэш")
    p.add_argument("--max-requests", type=int, help="Переопределить бюджет запросов")
    p.add_argument("--cache-dir", default=str(Path.home() / ".aviasales-cache"),
                   help="Папка кэша")
    return p


def run(
    args,
    now: dt.datetime,
    transport: Optional[Transport] = None,
    sleep=time.sleep,
) -> int:
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

    try:
        auth = parse_curl_auth(Path(args.curl).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Ошибка cURL-файла: {exc}", file=sys.stderr)
        return 2

    cache_dir = Path(args.cache_dir)
    probe_cache = ProbeCache(cache_dir / "probes.jsonl")
    client = SearchClient(auth=auth, transport=transport, sleep=sleep)
    planner = Planner(config, client, probe_cache, now=now, refresh=args.refresh)

    try:
        tickets = planner.plan()
    except ExpiredCurlError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    previous = latest_previous_offers(cache_dir, before_ts=now, trip=raw_config)
    report_md = render_markdown(tickets, top_n=args.top, previous_offers=previous)
    offers = offers_sorted_desc(tickets)

    write_snapshot(cache_dir, now, raw_config, offers, report_md)

    if args.out:
        Path(args.out).write_text(report_md, encoding="utf-8")
    print(report_md)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    return run(args, now=dt.datetime.now(), transport=default_transport())


if __name__ == "__main__":
    raise SystemExit(main())
