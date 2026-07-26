"""Клиент к реальному API Aviasales v3.2: двух-хостовый флоу START → POLL.

START (`tickets-api.aviasales.ru`) принимает весь маршрут (все `directions`
с фиксированными датами) одним запросом и возвращает `search_id` в теле
ответа. RESULTS (`tickets-api.eu-north-1.aviasales.ru`) поллится с этим
`search_id`, пока список билетов не стабилизируется или не исчерпан лимит
попыток. Сеть инжектируется через `Transport` — сам клиент сети не открывает
(кроме `default_transport`, где `curl_cffi` импортируется лениво).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .curl_auth import CurlAuth, classify_response
from .results_parser import extract_tickets
from .trip_model import Ticket

START_URL = "https://tickets-api.aviasales.ru/search/v2/start"
RESULTS_URL = "https://tickets-api.eu-north-1.aviasales.ru/search/v3.2/results"

# Диапазон человекоподобной паузы между сетевыми вызовами (сек).
_MIN_DELAY = 3.0
_MAX_DELAY = 8.0


@dataclass
class Response:
    status: int
    text: str

    def json(self) -> dict:
        return json.loads(self.text)


Transport = Callable[[str, str, dict, dict, Optional[str]], Response]


def _passengers_payload(passengers) -> dict:
    """Принимает либо готовый dict {"adults":..,"children":..,"infants":..},
    либо `trip_model.Passengers`-подобный объект с такими атрибутами."""
    if isinstance(passengers, dict):
        return passengers
    return {
        "adults": passengers.adults,
        "children": passengers.children,
        "infants": passengers.infants,
    }


def build_start_body(
    dated_directions,
    passengers,
    trip_class: str,
    market_code: str,
    currency_code: str,
) -> dict:
    """Строит тело POST START. `dated_directions` — список
    `(origin, destination, date_iso)` на весь маршрут (round-trip/multi-city —
    один поиск с несколькими направлениями, не отдельные one-way запросы)."""
    return {
        "search_params": {
            "directions": [
                {
                    "origin": origin,
                    "destination": destination,
                    "date": date_iso,
                    "is_origin_airport": False,
                    "is_destination_airport": False,
                }
                for origin, destination, date_iso in dated_directions
            ],
            "passengers": _passengers_payload(passengers),
            "trip_class": trip_class,
        },
        "market_code": market_code,
        "marker": "direct",
        "citizenship": "RU",
        "currency_code": currency_code,
        "languages": {"ru": 1},
        "debug": {"override_experiment_groups": {}},
        "brand": "AS",
        "client_features": {
            "direct_flights": True,
            "brand_ticket": False,
            "top_filters": True,
            "badges": False,
            "tour_tickets": True,
            "assisted": True,
        },
        "filters": {"initial_state": {}},
        "subscription_ticket_signatures": [],
        "is_internet_restricted": False,
    }


def _results_url(host: Optional[str]) -> str:
    """START отдаёт хост поллинга динамически (`results_url`, голый хост без
    схемы и пути) — регион меняется от поиска к поиску (live 2026-07-27:
    `ru-central-1` вместо прежнего `eu-north-1`, зашитый хост даёт 404).
    При отсутствии поля — исторический дефолт `RESULTS_URL`."""
    if not host:
        return RESULTS_URL
    return f"https://{host}/search/v3.2/results"


def _build_results_body(search_id: str) -> dict:
    return {
        "limit": 1000,
        "price_per_person": False,
        "search_by_airport": False,
        "filters_state": {},
        "search_id": search_id,
        "last_update_timestamp": 0,
    }


def _delay_seconds(attempt: int) -> float:
    # Детерминированная, но «человеческая» пауза (без random — тестируемо).
    span = _MAX_DELAY - _MIN_DELAY
    return _MIN_DELAY + span * ((attempt % 5) / 4.0)


def _total_tickets_count(payload) -> Optional[int]:
    """Читает `meta.total_tickets_count` из «сырого» JSON ответа RESULTS
    (список чанков или один чанк) — независимо от `results_parser`, который
    отдаёт уже смапленные/отфильтрованные `Ticket`, без meta. Суммирует по
    чанкам, если их несколько; `None`, если ни в одном чанке поля нет."""
    chunks = payload if isinstance(payload, list) else [payload]
    total = 0
    found = False
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("meta")
        if isinstance(meta, dict) and "total_tickets_count" in meta:
            total += meta["total_tickets_count"]
            found = True
    return total if found else None


def _parse_poll_response(resp: Response):
    """Разбирает тело ответа RESULTS-поллинга. HTTP 304 (или пустое/битое
    тело — сервер иногда отвечает так, когда с прошлого поллинга ничего не
    изменилось) не означает ошибку — это сигнал «без изменений», а не JSON
    для парсинга. Возвращает None в этом случае, иначе распарсенный JSON."""
    if resp.status == 304 or not resp.text.strip():
        return None
    try:
        return resp.json()
    except (ValueError, json.JSONDecodeError):
        return None


@dataclass
class SearchClient:
    auth: CurlAuth
    transport: Transport
    sleep: Callable[[float], None] = time.sleep
    max_poll: int = 6

    def _call(self, method: str, url: str, body: dict, attempt: int) -> Response:
        self.sleep(_delay_seconds(attempt))
        resp = self.transport(method, url, self.auth.headers, self.auth.cookies,
                              json.dumps(body))
        classify_response(resp.status, resp.text)
        return resp

    def search(
        self,
        dated_directions,
        passengers,
        trip_class: str,
        market_code: str,
        currency_code: str,
        baggage_required: bool = False,
    ) -> list[Ticket]:
        start_body = build_start_body(
            dated_directions, passengers, trip_class, market_code, currency_code,
        )
        start_resp = self._call("POST", START_URL, start_body, attempt=0)
        start_payload = start_resp.json()
        search_id = start_payload["search_id"]
        results_url = _results_url(start_payload.get("results_url"))

        # Сервер заполняет поиск асинхронно (до ~200 билетов на ответ, даже
        # если реально их больше — см. meta.total_tickets_count), поэтому
        # поллим RESULTS, пока:
        #   (a) результат пуст, ИЛИ
        #   (b) meta.total_tickets_count известен и больше числа уже
        #       распарсенных билетов, И (c) ещё не было двух подряд опросов
        #       с одинаковым числом билетов.
        # Т.е. останавливаемся раньше max_poll только когда результат непуст
        # И (число билетов достигло total_tickets_count ИЛИ два опроса подряд
        # дали одно и то же число). HTTP 304 / пустое-нераспарсиваемое тело
        # трактуем как «без изменений» — как ещё один опрос с тем же числом.
        # По исчерпании max_poll возвращаем последний (возможно неполный)
        # результат.
        tickets: list[Ticket] = []
        previous_count: Optional[int] = None
        for attempt in range(1, self.max_poll + 1):
            results_resp = self._call(
                "POST", results_url, _build_results_body(search_id), attempt=attempt,
            )
            payload = _parse_poll_response(results_resp)

            if payload is None:
                # 304/пустое тело: без изменений с прошлого опроса.
                if previous_count is not None:
                    break  # предыдущий непустой результат уже стабилен
                continue

            parsed = extract_tickets(payload, baggage_required=baggage_required)
            if not parsed:
                previous_count = None
                continue

            tickets = parsed
            total = _total_tickets_count(payload)
            reached_total = total is not None and len(parsed) >= total
            stabilized = previous_count == len(parsed)
            previous_count = len(parsed)
            if reached_total or stabilized:
                break
        return tickets


def default_transport() -> Transport:
    """Боевой транспорт с TLS-имперсонацией браузера. `curl_cffi` импортируется
    лениво — для тестов/разработки он не нужен и не тянется в зависимости."""
    from curl_cffi import requests as cffi_requests  # noqa: PLC0415

    def _transport(method, url, headers, cookies, body) -> Response:
        r = cffi_requests.request(
            method, url, headers=headers, cookies=cookies,
            data=body, impersonate="chrome", timeout=30,
        )
        return Response(status=r.status_code, text=r.text)

    return _transport
