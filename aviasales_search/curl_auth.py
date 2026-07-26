"""Извлечение авторизации (headers+cookies) из дампа `Copy as cURL (bash)`.

Под реальный API (v3.2) cURL — ЕДИНСТВЕННЫЙ источник заголовков и куки браузера
(WAF-токен, auid и т.п.). Тело запроса cURL нас не интересует — его строит
сам инструмент (см. R4), поэтому `parse_curl_auth` игнорирует URL/метод/тело.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field


class ExpiredCurlError(RuntimeError):
    """cURL/куки протухли или сработала защита от ботов — нужен свежий cURL."""


@dataclass
class CurlAuth:
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


_BLOCK_MARKERS = ("captcha", "attention required", "just a moment")


def _next_token(tokens: list[str], i: int, flag: str) -> str:
    """Безопасно берёт токен после флага, кидая ValueError если его нет
    (устойчивость к обрезанному/некорректно скопированному cURL)."""
    if i + 1 >= len(tokens):
        raise ValueError(f"в cURL у флага {flag} нет значения — команда обрезана?")
    return tokens[i + 1]


def _parse_cookie_header(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def parse_curl_auth(text: str) -> CurlAuth:
    """Парсит `-H`/`--header` и `-b`/`--cookie` (или cookie-заголовок) из
    дампа `Copy as cURL (bash)`. URL, метод и тело запроса игнорируются —
    под реальный API тело строит сам инструмент (R4)."""
    joined = text.replace("\\\n", " ").strip()
    tokens = shlex.split(joined)
    if not tokens or tokens[0] != "curl":
        raise ValueError("ожидалась команда, начинающаяся с 'curl'")

    headers: dict[str, str] = {}
    explicit_cookies: dict[str, str] = {}

    i = 1
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("-H", "--header"):
            raw = _next_token(tokens, i, tok)
            if ":" in raw:
                k, v = raw.split(":", 1)
                headers[k.strip().lower()] = v.strip()
            # заголовки без ':' (напр. 'authorization;' для пустого значения,
            # которое Chrome иногда генерирует) игнорируем — значения всё равно нет.
            i += 1
        elif tok in ("-b", "--cookie"):
            explicit_cookies.update(_parse_cookie_header(_next_token(tokens, i, tok)))
            i += 1
        # URL/метод/тело (-X/--request, -d/--data*, голый URL) сознательно
        # не разбираем — R2 отвечает только за headers+cookies.
        i += 1

    cookies = explicit_cookies
    if not cookies and "cookie" in headers:
        cookies = _parse_cookie_header(headers["cookie"])

    return CurlAuth(headers=headers, cookies=cookies)


def classify_response(status: int, text: str) -> None:
    """Кидает ExpiredCurlError, если ответ похож на протухший cURL/бан:
    401/403/429/5xx или маркер anti-bot challenge в теле."""
    if status in (401, 403, 429) or status >= 500:
        raise ExpiredCurlError(
            f"Aviasales вернул статус {status} — cURL/куки протухли или сработала "
            f"защита. Обновите cURL (см. SKILL.md, раздел «Обновление cURL»)."
        )
    lowered = text.lower()
    if any(m in lowered for m in _BLOCK_MARKERS):
        raise ExpiredCurlError(
            "В ответе Aviasales маркер защиты от ботов (captcha/challenge). "
            "Обновите cURL (см. SKILL.md)."
        )
