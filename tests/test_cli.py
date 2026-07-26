import datetime as dt
import json
from pathlib import Path

from aviasales_search.cli import build_arg_parser, run
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


def _mock_transport(start_status=200, start_body=None, results_status=200, results_text=None):
    start_body = start_body if start_body is not None else {"search_id": "abc123"}
    results_text = results_text if results_text is not None else FIXTURE_RESULTS.read_text()

    def transport(method, url, headers, cookies, body):
        if url == START_URL:
            return Response(status=start_status, text=json.dumps(start_body))
        if url == RESULTS_URL:
            return Response(status=results_status, text=results_text)
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
