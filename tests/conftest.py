import pytest


@pytest.fixture(autouse=True)
def _isolate_cwd(tmp_path, monkeypatch):
    """CLI пишет отчёты/кэш относительно cwd, когда --out/--cache-dir не
    заданы (например, тесты кодов возврата ошибок — там до записи файлов
    дело не доходит, но `out_path.parent.mkdir(...)` в cli.py срабатывает
    ещё до проверки cURL). Без изоляции такие тесты мусорят в настоящем
    `reports/`/`cache/` репозитория. Автоприменяемый chdir в tmp_path
    страхует все тесты разом, включая будущие, без ручного --out в каждом."""
    monkeypatch.chdir(tmp_path)
