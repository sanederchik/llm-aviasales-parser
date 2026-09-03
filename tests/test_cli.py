import datetime as dt
import json
from pathlib import Path

from aviasales_search.cli import build_arg_parser, main, run
from aviasales_search.csv_report import BOM
from aviasales_search.search_client import RESULTS_URL, START_URL, Response

FIXTURE_CURL = Path(__file__).parent / "fixtures" / "curl_sample.txt"
FIXTURE_RESULTS = Path(__file__).parent / "fixtures" / "results_v32_sample.json"


def _write_config(tmp_path, **overrides):
    cfg = {
        "directions": [
            {
                "from": "MOW", "to": "DPS",
                "date_window": {"earliest": "2026-09-15", "latest": "2026-09-15"},
            },
            {
                "from": "DPS", "to": "MOW",
                "date_window": {"earliest": "2026-12-15", "latest": "2026-12-15"},
            },
        ],
        "passengers": {"adults": 2},
        "search_budget": {"max_requests": 5, "date_samples_per_direction": 1},
    }
    cfg.update(overrides)
    p = tmp_path / "trip.json"
    p.write_text(json.dumps(cfg))
    return p


def _one_way_results_text(results_text):
    """Срез multi-city фикстуры до одного направления (segments[:1]) — имитация
    ответа one-way поиска Фазы 1 (у настоящего one-way ответа одно направление;
    combo-фикстура несёт два, и без среза их отфильтровал бы passes_itinerary
    одноплечевого под-итинерария)."""
    data = json.loads(results_text)
    for chunk in data:
        for t in chunk.get("tickets", []):
            if t.get("segments"):
                t["segments"] = t["segments"][:1]
    return json.dumps(data)


def _mock_transport(start_status=200, start_body=None, results_status=200, results_text=None):
    start_body = start_body if start_body is not None else {"search_id": "abc123"}
    results_text = results_text if results_text is not None else FIXTURE_RESULTS.read_text()
    one_way_text = _one_way_results_text(results_text)
    state = {"one_way": False}

    def transport(method, url, headers, cookies, body):
        if url == START_URL:
            # Фаза 1 шлёт one-way START (одно направление) — запоминаем, чтобы на
            # последующем RESULTS отдать одноплечевой срез; Фаза 2 шлёт multi-city.
            directions = json.loads(body)["search_params"]["directions"]
            state["one_way"] = len(directions) == 1
            return Response(status=start_status, text=json.dumps(start_body))
        if url == RESULTS_URL:
            text = one_way_text if state["one_way"] else results_text
            return Response(status=results_status, text=text)
        raise AssertionError(f"unexpected url: {url}")

    return transport


def test_run_happy_path_writes_report_and_snapshot(tmp_path):
    parser = build_arg_parser()
    out = tmp_path / "report.md"
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--out", str(out),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 0
    assert out.exists()
    assert "Результаты поиска" in out.read_text()

    json_path = tmp_path / "json" / "report.json"
    csv_path = tmp_path / "csv" / "report.csv"
    data = json.loads(json_path.read_text())
    assert data["schema_version"] == 1
    assert data["trip"]["directions"][0]["from"] == "MOW"
    assert len(data["offers"]) >= 1
    assert all("legs" in d for o in data["offers"] for d in o["directions"])
    csv_text = csv_path.read_text()
    assert csv_text.startswith(BOM)
    header = csv_text.lstrip(BOM).splitlines()[0]
    assert header.startswith("цена_руб,багаж,багаж_кг,mow_dps_дата")
    assert len(csv_text.strip().splitlines()) == 1 + len(data["offers"])

    runs = list((tmp_path / "cache" / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "offers.jsonl").exists()
    assert (runs[0] / "trip.json").exists()
    assert (runs[0] / "report.md").exists()


def test_run_persists_max_requests_override_in_snapshot(tmp_path):
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--out", str(tmp_path / "report.md"),  # без --out отчёт утёк бы в reports/ репозитория
        "--cache-dir", str(tmp_path / "cache"),
        "--max-requests", "3",
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 0
    runs = list((tmp_path / "cache" / "runs").iterdir())
    assert len(runs) == 1
    trip = json.loads((runs[0] / "trip.json").read_text())
    assert trip["search_budget"]["max_requests"] == 3


def test_run_returns_2_on_expired_curl(tmp_path):
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(start_status=403), sleep=lambda _: None,
    )
    assert code == 2


def test_run_returns_1_on_broken_config(tmp_path):
    p = tmp_path / "trip.json"
    p.write_text(json.dumps({"passengers": {"adults": 2}}))  # missing 'directions'

    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(p),
        "--curl", str(FIXTURE_CURL),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 1


def test_run_returns_1_on_invalid_json(tmp_path):
    p = tmp_path / "trip.json"
    p.write_text("{not valid json")

    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(p),
        "--curl", str(FIXTURE_CURL),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 1


def test_run_returns_1_on_missing_config_file(tmp_path):
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(tmp_path / "does-not-exist.json"),
        "--curl", str(FIXTURE_CURL),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 1


def test_run_returns_2_on_missing_curl_file(tmp_path):
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(tmp_path / "does-not-exist.txt"),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 2


def test_run_returns_2_on_malformed_curl_text(tmp_path):
    bad_curl = tmp_path / "bad_curl.txt"
    bad_curl.write_text("this is not a curl command at all")

    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(bad_curl),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 2


def test_run_without_transport_raises():
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", "irrelevant.json",
        "--curl", "irrelevant.txt",
    ])
    try:
        run(args, now=dt.datetime(2026, 7, 26, 12, 0), transport=None)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "transport" in str(exc)


def test_run_second_invocation_reports_price_delta(tmp_path):
    """Two runs against the same config/cache-dir: the second report must show
    a delta against the first run's best price (via latest_previous_offers)."""
    config_path = _write_config(tmp_path)
    cache_dir = tmp_path / "cache"
    parser = build_arg_parser()

    args1 = parser.parse_args([
        "--config", str(config_path),
        "--curl", str(FIXTURE_CURL),
        "--out", str(tmp_path / "report1.md"),
        "--cache-dir", str(cache_dir),
    ])
    code1 = run(
        args1, now=dt.datetime(2026, 7, 26, 10, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code1 == 0

    args2 = parser.parse_args([
        "--config", str(config_path),
        "--curl", str(FIXTURE_CURL),
        "--out", str(tmp_path / "report2.md"),
        "--cache-dir", str(cache_dir),
        "--refresh",
    ])
    code2 = run(
        args2, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code2 == 0

    report2 = (tmp_path / "report2.md").read_text()
    assert "Лучшая цена" in report2
    assert "было" in report2


def test_run_writes_progress_log_to_run_dir_and_stderr(tmp_path, capsys):
    parser = build_arg_parser()
    out = tmp_path / "report.md"
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--out", str(out),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(
        args, now=dt.datetime(2026, 7, 26, 12, 0),
        transport=_mock_transport(), sleep=lambda _: None,
    )
    assert code == 0

    runs = list((tmp_path / "cache" / "runs").iterdir())
    assert len(runs) == 1
    log_text = (runs[0] / "search.log").read_text()
    lines = log_text.splitlines()
    # Две фазы, два заголовка: Фаза 1 (свип — 2 даты, по одной на плечо) и
    # Фаза 2 (верификация — 1 комбо в ранкинге).
    assert lines[0].startswith("Комбинаций дат: 2")  # свип: 2 даты
    assert "Комбинаций дат: 1" in log_text            # верификация: 1 комбо
    assert "MOW→DPS 2026-09-15" in log_text           # одноплечевая строка свипа
    assert "DPS→MOW 2026-12-15" in log_text            # верификация комбо целиком

    captured = capsys.readouterr()
    assert lines[0] in captured.err  # прогресс дублируется в stderr
    assert "Комбинаций дат" not in captured.out  # отчёт в stdout не замусорен
    assert "Комбинаций дат" not in out.read_text()


def test_run_defaults_write_report_and_cache_into_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parser = build_arg_parser()
    args = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
    ])
    code = run(args, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=_mock_transport(), sleep=lambda _: None)
    assert code == 0
    run_dir = tmp_path / "reports" / "2026-07-26__mow-dps-2026-09"
    report = run_dir / "mow-dps-2026-09.md"
    assert report.exists()
    assert "Результаты поиска" in report.read_text()
    assert (run_dir / "json" / "mow-dps-2026-09.json").exists()
    assert (run_dir / "csv" / "mow-dps-2026-09.csv").exists()
    assert (tmp_path / "cache" / "probes.jsonl").exists()
    assert (tmp_path / "cache" / "runs").is_dir()


def test_default_report_path_multicity_slug(tmp_path):
    from aviasales_search.cli import default_report_path
    from aviasales_search.trip_model import parse_config
    config = parse_config({
        "directions": [
            {"from": "MOW", "to": "IST",
             "date_window": {"earliest": "2026-09-13", "latest": "2026-09-13"}},
            {"from": "IST", "to": "DPS",
             "date_window": {"earliest": "2026-10-20", "latest": "2026-10-20"}},
            {"from": "DPS", "to": "MOW",
             "date_window": {"earliest": "2026-11-15", "latest": "2026-11-15"}},
        ],
    })
    # Дата поиска (search_date) — не дата вылета; берётся отдельно, задаёт
    # префикс папки прогона, чтобы повторные поиски того же маршрута не
    # затирали друг друга.
    path = default_report_path(config, search_date=dt.date(2026, 8, 5))
    assert str(path) == "reports/2026-08-05__mow-ist-dps-2026-09/mow-ist-dps-2026-09.md"


def test_top_default_is_unlimited():
    args = build_arg_parser().parse_args(["--config", "c", "--curl", "k"])
    assert args.top is None


def test_render_from_rebuilds_md_and_csv_without_network(tmp_path):
    # Сначала обычный прогон с мок-транспортом — получаем JSON.
    parser = build_arg_parser()
    out1 = tmp_path / "report.md"
    args1 = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--out", str(out1),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    assert run(args1, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=_mock_transport(), sleep=lambda _: None) == 0
    json_path = tmp_path / "json" / "report.json"
    original_json = json_path.read_text()

    # Теперь пересборка из JSON: без --config, без --curl, transport=None.
    out2 = tmp_path / "rebuilt" / "report2.md"
    args2 = parser.parse_args([
        "--render-from", str(json_path),
        "--out", str(out2),
    ])
    assert run(args2, now=dt.datetime(2026, 7, 27, 9, 0), transport=None) == 0
    assert "Результаты поиска" in out2.read_text()
    csv2 = tmp_path / "rebuilt" / "csv" / "report2.csv"
    assert csv2.read_text().startswith(BOM)
    # generated_at сохраняется из исходного JSON, а не перештамповывается.
    rebuilt = json.loads((tmp_path / "rebuilt" / "json" / "report2.json").read_text())
    assert rebuilt["generated_at"] == json.loads(original_json)["generated_at"]


def test_render_from_default_out_path_from_trip(tmp_path, monkeypatch):
    # Прогон → JSON, затем пересборка без --out: путь по умолчанию из trip.
    parser = build_arg_parser()
    monkeypatch.chdir(tmp_path)
    args1 = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
    ])
    assert run(args1, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=_mock_transport(), sleep=lambda _: None) == 0
    run_dir = tmp_path / "reports" / "2026-07-26__mow-dps-2026-09"
    json_path = run_dir / "json" / "mow-dps-2026-09.json"

    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    args2 = parser.parse_args(["--render-from", str(json_path)])
    # Пересобираем 27-го, но папка называется по исходной дате поиска (26-е,
    # из generated_at в JSON) — не по дате самой пересборки.
    assert run(args2, now=dt.datetime(2026, 7, 27, 9, 0), transport=None) == 0
    other_run_dir = other / "reports" / "2026-07-26__mow-dps-2026-09"
    assert (other_run_dir / "mow-dps-2026-09.md").exists()
    assert (other_run_dir / "csv" / "mow-dps-2026-09.csv").exists()


def test_render_from_returns_1_on_missing_file(tmp_path, capsys):
    args = build_arg_parser().parse_args(
        ["--render-from", str(tmp_path / "no-such.json")])
    assert run(args, now=dt.datetime(2026, 7, 27, 9, 0), transport=None) == 1
    assert "Ошибка" in capsys.readouterr().err


def test_render_from_returns_1_on_wrong_schema(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 99, "offers": []}))
    args = build_arg_parser().parse_args(["--render-from", str(bad)])
    assert run(args, now=dt.datetime(2026, 7, 27, 9, 0), transport=None) == 1
    assert "Ошибка" in capsys.readouterr().err


def test_search_mode_without_config_or_curl_returns_1(tmp_path, capsys):
    args = build_arg_parser().parse_args(["--cache-dir", str(tmp_path)])
    code = run(args, now=dt.datetime(2026, 7, 27, 9, 0),
               transport=_mock_transport(), sleep=lambda _: None)
    assert code == 1
    assert "--config" in capsys.readouterr().err


def test_main_render_from_does_not_require_live_transport(tmp_path, monkeypatch):
    # default_transport() импортирует curl_cffi — в render-режиме сеть не нужна,
    # поэтому main() не должен его вызывать вовсе. Замоняем default_transport так,
    # чтобы любой его вызов детерминированно валил тест (даже если curl_cffi
    # установлен в тестовом окружении и падение ModuleNotFoundError не проявится).
    monkeypatch.setattr(
        "aviasales_search.cli.default_transport",
        lambda: (_ for _ in ()).throw(
            AssertionError("default_transport не должен вызываться в render-режиме")),
    )

    # Сначала обычный прогон с мок-транспортом (через run(), не main()) — получаем JSON.
    parser = build_arg_parser()
    args1 = parser.parse_args([
        "--config", str(_write_config(tmp_path)),
        "--curl", str(FIXTURE_CURL),
        "--out", str(tmp_path / "report.md"),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    assert run(args1, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=_mock_transport(), sleep=lambda _: None) == 0
    json_path = tmp_path / "json" / "report.json"

    out2 = tmp_path / "rebuilt" / "report2.md"
    code = main(["--render-from", str(json_path), "--out", str(out2)])
    assert code == 0
    assert out2.exists()
    assert "Результаты поиска" in out2.read_text()


def test_run_intermediate_report_survives_mid_sweep_ban(tmp_path):
    """Фаза 1 свипит плечи по датам. Первая дата уходит в сеть штатно, на второй
    START сервер отвечает 403 (бан). run() возвращает 2, но живой промежуточный
    отчёт (legs-*) уже содержит собранное до обрыва — Фаза 2 не начинается."""
    parser = build_arg_parser()
    out = tmp_path / "report.md"
    cfg = _write_config(
        tmp_path,
        directions=[
            {"from": "MOW", "to": "DPS",
             "date_window": {"earliest": "2026-09-15", "latest": "2026-09-16"}},
            {"from": "DPS", "to": "MOW",
             "date_window": {"earliest": "2026-12-15", "latest": "2026-12-15"}},
        ],
        search_budget={"max_requests": 5, "date_samples_per_direction": 2},
    )
    good = _mock_transport()
    starts = {"n": 0}

    def transport(method, url, headers, cookies, body):
        if url == START_URL:
            starts["n"] += 1
            if starts["n"] > 1:
                return Response(status=403, text="banned")
        return good(method, url, headers, cookies, body)

    args = parser.parse_args([
        "--config", str(cfg),
        "--curl", str(FIXTURE_CURL),
        "--out", str(out),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(args, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=transport, sleep=lambda _: None)
    assert code == 2

    # Промежуточный отчёт (Фаза 1) записан живьём до бана: первая дата плеча 0
    # успела собраться. Финального отчёта нет — Фаза 2 не запускалась.
    legs_md = tmp_path / "legs-report.md"
    legs_json = tmp_path / "json" / "legs-report.json"
    assert legs_md.exists()
    assert "## MOW→DPS" in legs_md.read_text()
    data = json.loads(legs_json.read_text())
    assert data["legs"][0]["dates"]  # плечо 0, дата 15.09 — собрана
    assert not out.exists()  # финальный отчёт не создан (верификация не дошла)


def test_run_passes_config_delays_to_client(tmp_path):
    """Паузы из search_budget.request_delay_seconds доходят до SearchClient:
    с фиксированным min=max каждый sleep равен этому значению."""
    cfg = _write_config(tmp_path, search_budget={
        "max_requests": 5, "date_samples_per_direction": 1,
        "request_delay_seconds": {"min": 9.0, "max": 9.0},
    })
    sleeps = []
    args = build_arg_parser().parse_args([
        "--config", str(cfg),
        "--curl", str(FIXTURE_CURL),
        "--out", str(tmp_path / "report.md"),
        "--cache-dir", str(tmp_path / "cache"),
    ])
    code = run(args, now=dt.datetime(2026, 7, 26, 12, 0),
               transport=_mock_transport(), sleep=sleeps.append)
    assert code == 0
    assert sleeps and all(s == 9.0 for s in sleeps)


def test_verify_top_flag_default_and_parse():
    args = build_arg_parser().parse_args(["--config", "c.json", "--curl", "u.txt"])
    assert args.verify_top == 60
    args2 = build_arg_parser().parse_args(
        ["--config", "c.json", "--curl", "u.txt", "--verify-top", "10"])
    assert args2.verify_top == 10


def test_legs_report_path_prefixes_legs():
    from aviasales_search.cli import legs_report_path
    from aviasales_search.trip_model import parse_config
    cfg = parse_config({
        "directions": [
            {"from": "MOW", "to": "ALA",
             "date_window": {"earliest": "2026-09-10", "latest": "2026-09-11"}},
            {"from": "ALA", "to": "TAS",
             "date_window": {"earliest": "2026-09-15", "latest": "2026-09-16"}},
        ],
    })
    path = legs_report_path(cfg, search_date=dt.date(2026, 8, 5))
    assert path == Path("reports") / "2026-08-05__mow-ala-tas-2026-09" / "legs-mow-ala-tas-2026-09.md"
