import io

from aviasales_search.progress import (
    ComboEvent,
    ProgressLog,
    format_combo_line,
    format_header,
)

# --------------------------- builders ---------------------------


def _event(**overrides):
    base = dict(
        index=12,
        total=300,
        dated_dirs=[("MOW", "DPS", "2026-09-07"), ("DPS", "MOW", "2026-11-01")],
        source="сеть",
        found=60,
        passed=8,
        best_price=145231,
        running_min=145231,
        improved=False,
    )
    base.update(overrides)
    return ComboEvent(**base)


class _FlushCountingSink(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_calls = 0

    def flush(self):
        self.flush_calls += 1
        super().flush()


# --------------------------- format_header ---------------------------


def test_format_header_lists_totals_budget_and_ttl():
    assert format_header(300, 400, 60) == (
        "Комбинаций дат: 300, сетевой бюджет: 400, кэш TTL: 60 мин"
    )


# --------------------------- format_combo_line ---------------------------


def test_format_combo_line_network_source():
    line = format_combo_line(_event())
    assert line == (
        "[ 12/300   4.0%] MOW→DPS 2026-09-07 | DPS→MOW 2026-11-01"
        " | сеть | найдено 60 | прошло 8 | лучшая 145 231 ₽"
        " | мин. с начала 145 231 ₽"
    )


def test_format_combo_line_marks_improvement_with_star():
    line = format_combo_line(_event(best_price=139990, running_min=139990, improved=True))
    assert line.endswith("лучшая 139 990 ₽ | мин. с начала 139 990 ₽ ★")


def test_format_combo_line_no_running_min_yet():
    line = format_combo_line(_event(found=5, passed=0, best_price=None, running_min=None))
    assert line.endswith("лучшая — | мин. с начала —")


def test_format_combo_line_cache_source():
    line = format_combo_line(_event(source="кэш"))
    assert " | кэш | " in line


def test_format_combo_line_skipped_combo_uses_dashes():
    line = format_combo_line(
        _event(index=299, source="пропуск", found=None, passed=None, best_price=None,
               running_min=141000)
    )
    assert line == (
        "[299/300  99.7%] MOW→DPS 2026-09-07 | DPS→MOW 2026-11-01"
        " | пропуск | найдено — | прошло — | лучшая — | мин. с начала 141 000 ₽"
    )


def test_format_combo_line_no_passing_tickets_has_dash_price():
    line = format_combo_line(_event(found=5, passed=0, best_price=None, running_min=140000))
    assert line.endswith("найдено 5 | прошло 0 | лучшая — | мин. с начала 140 000 ₽")


def test_format_combo_line_index_aligned_to_total_width():
    line = format_combo_line(_event(index=7, total=300))
    assert line.startswith("[  7/300")


def test_format_combo_line_single_direction():
    line = format_combo_line(
        _event(dated_dirs=[("MOW", "LED", "2026-09-15")], index=1, total=2,
               found=3, passed=3, best_price=3462)
    )
    assert "MOW→LED 2026-09-15 | сеть" in line
    assert line.startswith("[1/2  50.0%]")


# --------------------------- ProgressLog ---------------------------


def test_progress_log_writes_header_and_lines_to_all_sinks():
    a, b = io.StringIO(), io.StringIO()
    log = ProgressLog([a, b])
    log.start(300, 400, 60)
    log.emit(_event())
    for sink in (a, b):
        text = sink.getvalue()
        lines = text.splitlines()
        assert lines[0] == format_header(300, 400, 60)
        assert lines[1] == format_combo_line(_event())
        assert text.endswith("\n")
    assert a.getvalue() == b.getvalue()


def test_progress_log_flushes_after_every_write():
    sink = _FlushCountingSink()
    log = ProgressLog([sink])
    log.start(300, 400, 60)
    log.emit(_event())
    assert sink.flush_calls == 2
