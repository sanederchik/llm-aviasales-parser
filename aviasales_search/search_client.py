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
# Дефолтные паузы перед HTTP-вызовами; переопределяются конфигом
# (search_budget.request_delay_seconds) через поля SearchClient.
_MIN_DELAY = 0.5
_MAX_DELAY = 1.0

# Живая проверка 2026-08-07: при активных filters_state индекс наполняется
# асинхронно (meta.total_tickets_count растёт между поллами, первые поллы
# пустые). Пауза перед поллом после пустого ответа — не меньше этого порога,
# чтобы не долбить сервер, пока он ещё считает билеты.
_COLD_EMPTY_DELAY_FLOOR = 2.0


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
        # Обязательно для WAF start-хоста (живая проверка 2026-07-28): тело без
        # experiment_groups получает «access denied» даже с валидными куками и
        # x-aws-waf-token; достаточно этих двух bot-scoring ключей из браузера.
        "experiment_groups": {
            "search-exp-smartCaptcha": "smart-captcha:invisible",
            "search-exp-botScoring": "v2",
        },
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


def _build_results_body(search_id: str, filters_state: Optional[dict] = None) -> dict:
    return {
        "limit": 1000,
        "price_per_person": False,
        "search_by_airport": False,
        "filters_state": filters_state or {},
        "search_id": search_id,
        "last_update_timestamp": 0,
    }


def _delay_seconds(attempt: int, min_delay: float = _MIN_DELAY,
                   max_delay: float = _MAX_DELAY) -> float:
    # Детерминированная, но «человеческая» пауза (без random — тестируемо).
    span = max_delay - min_delay
    return min_delay + span * ((attempt % 5) / 4.0)


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


def _filtered_tickets_count(payload) -> Optional[int]:
    """Читает `meta.filtered_tickets_count` — присутствует в ответе RESULTS,
    только когда активны серверные `filters_state` (живая проверка
    2026-08-07: `{"filtered_tickets_count": 18, "total_tickets_count": 444}`).
    Суммирует по чанкам, если их несколько; `None`, если поля нет ни в одном
    чанке (без активных фильтров сервер его не отдаёт вовсе — отличать от
    «есть и равно 0»)."""
    chunks = payload if isinstance(payload, list) else [payload]
    total = 0
    found = False
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        meta = chunk.get("meta")
        if isinstance(meta, dict) and "filtered_tickets_count" in meta:
            total += meta["filtered_tickets_count"]
            found = True
    return total if found else None


def _expected_tickets_count(payload) -> Optional[int]:
    """Ожидаемое число билетов для стоп-условия «набрали всё»: при активных
    серверных фильтрах ориентир — `meta.filtered_tickets_count` (сырой
    `total_tickets_count` их не учитывает и может быть в разы больше);
    без фильтров (поле отсутствует) — как раньше, `meta.total_tickets_count`."""
    filtered = _filtered_tickets_count(payload)
    if filtered is not None:
        return filtered
    return _total_tickets_count(payload)


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
    # Верхняя граница поллинга «холодного старта»: пока результат пуст И
    # meta.total_tickets_count ещё растёт (индекс наполняется — живая
    # проверка 2026-08-07), поллим сверх max_poll, но не более этого числа
    # попыток суммарно.
    max_poll_cold: int = 15
    delay_min: float = _MIN_DELAY
    delay_max: float = _MAX_DELAY

    def _call(self, method: str, url: str, body: dict, attempt: int,
              delay_floor: Optional[float] = None) -> Response:
        delay = _delay_seconds(attempt, self.delay_min, self.delay_max)
        if delay_floor is not None:
            delay = max(delay, delay_floor)
        self.sleep(delay)
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
        min_baggage_weight_kg: Optional[int] = None,
        filters_state: Optional[dict] = None,
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
        # И (число билетов достигло «ожидаемого» — meta.filtered_tickets_count,
        # если сервер его отдаёт при активных фильтрах, иначе
        # total_tickets_count — ИЛИ два опроса подряд дали одно и то же
        # число). HTTP 304 / пустое-нераспарсиваемое тело трактуем как «без
        # изменений» — как ещё один опрос с тем же числом.
        #
        # Холодный старт с активными filters_state (живая проверка
        # 2026-08-07): индекс наполняется десятки секунд, первые поллы
        # пустые при РАСТУЩЕМ total_tickets_count (видели 444→846 между
        # двумя поллами подряд). Пустые поллы сами по себе — не
        # стабилизация: пока результат пуст и total растёт, поллим сверх
        # max_poll (до max_poll_cold суммарно), с паузой перед следующим
        # поллом не меньше _COLD_EMPTY_DELAY_FLOOR. Если же
        # filtered_tickets_count пришёл нулевым и total не вырос два полла
        # подряд — это честный пустой результат, выходим раньше, не тратя
        # оставшийся бюджет.
        #
        # По исчерпании лимита возвращаем последний (возможно неполный)
        # результат.
        tickets: list[Ticket] = []
        previous_count: Optional[int] = None
        previous_total: Optional[int] = None
        stagnant_filtered_empty_polls = 0
        poll_was_empty = False  # для паузы: предыдущий полл вернул 0 билетов
        hard_limit = self.max_poll
        attempt = 0
        while attempt < hard_limit:
            attempt += 1
            delay_floor = _COLD_EMPTY_DELAY_FLOOR if poll_was_empty else None
            results_resp = self._call(
                "POST", results_url, _build_results_body(search_id, filters_state),
                attempt=attempt, delay_floor=delay_floor,
            )
            payload = _parse_poll_response(results_resp)

            if payload is None:
                # 304/пустое тело: без изменений с прошлого опроса — не тот
                # же случай, что «пустой JSON-ответ с растущим total», паузу
                # не форсируем.
                poll_was_empty = False
                if previous_count is not None:
                    break  # предыдущий непустой результат уже стабилен
                continue

            parsed = extract_tickets(
                payload, baggage_required=baggage_required,
                min_baggage_weight_kg=min_baggage_weight_kg,
            )
            total_raw = _total_tickets_count(payload)

            if not parsed:
                poll_was_empty = True
                previous_count = None
                grown = (
                    previous_total is not None and total_raw is not None
                    and total_raw > previous_total
                )
                not_started_yet = not total_raw  # None или 0 — см. ниже
                if total_raw is not None:
                    previous_total = total_raw
                if grown or not_started_yet:
                    # Индекс ещё наполняется (total растёт) ИЛИ поиск ещё не
                    # начал наполняться вовсе (total==0/неизвестен — живой
                    # холодный прогон 2026-08-07: первые секунды сервер
                    # отдаёт filtered=0 И total=0, это НЕ «ни один билет не
                    # прошёл фильтр», а «индекс пуст пока»; та же комбинация
                    # тёплой отдаёт билеты первым же поллом). Ни то, ни
                    # другое не стабилизация и не повод для стагнации —
                    # продолжаем сверх max_poll (но не более max_poll_cold
                    # суммарно).
                    stagnant_filtered_empty_polls = 0
                    if self.max_poll_cold > hard_limit:
                        hard_limit = self.max_poll_cold
                    continue
                if _filtered_tickets_count(payload) == 0:
                    # Сюда попадаем только когда total_raw>0 (иначе сработал
                    # бы not_started_yet выше) — индекс уже наполнен, и
                    # ни один билет реально не прошёл фильтр.
                    stagnant_filtered_empty_polls += 1
                    if stagnant_filtered_empty_polls >= 2:
                        break  # честный пустой результат при активном фильтре
                else:
                    # Серия разорвана: filtered присутствует и ненулевой, или
                    # вовсе отсутствует — «два ПОДРЯД» больше не в счёт.
                    stagnant_filtered_empty_polls = 0
                continue

            poll_was_empty = False
            stagnant_filtered_empty_polls = 0
            tickets = parsed
            if total_raw is not None:
                previous_total = total_raw
            expected = _expected_tickets_count(payload)
            reached_total = expected is not None and len(parsed) >= expected
            stabilized = previous_count == len(parsed)
            previous_count = len(parsed)
            if reached_total or stabilized:
                break
        return tickets


# Транзиентные сетевые сбои (повисший DNS, обрыв соединения) ретраятся с
# паузами, а не роняют весь многочасовой перебор (живой сбой 2026-07-29:
# curl error 28 на одном поллинге убил прогон из 300 комбинаций).
_TRANSIENT_ATTEMPTS = 3
_TRANSIENT_BACKOFF_SECONDS = (2.0, 5.0)


def default_transport(sleep: Callable[[float], None] = time.sleep) -> Transport:
    """Боевой транспорт с TLS-имперсонацией браузера. `curl_cffi` импортируется
    лениво — для тестов/разработки он не нужен и не тянется в зависимости."""
    from curl_cffi import requests as cffi_requests  # noqa: PLC0415

    transient = (
        cffi_requests.exceptions.Timeout,
        cffi_requests.exceptions.ConnectionError,
    )

    def _transport(method, url, headers, cookies, body) -> Response:
        for attempt in range(_TRANSIENT_ATTEMPTS):
            try:
                r = cffi_requests.request(
                    method, url, headers=headers, cookies=cookies,
                    data=body, impersonate="chrome", timeout=30,
                )
                return Response(status=r.status_code, text=r.text)
            except transient:
                if attempt == _TRANSIENT_ATTEMPTS - 1:
                    raise
                sleep(_TRANSIENT_BACKOFF_SECONDS[attempt])
        raise AssertionError("unreachable")  # цикл всегда возвращает или кидает

    return _transport
