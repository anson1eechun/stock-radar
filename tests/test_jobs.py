import pytest

from stock_radar import jobs
from stock_radar.db import connect


def test_error_notifications_only_on_state_change(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    sent = []
    monkeypatch.setattr(jobs, "connect", lambda: connect(path))
    monkeypatch.setattr(jobs.discord, "send", lambda text, buttons=None: sent.append(text))
    outcomes = iter([ConnectionError("dns"), ConnectionError("dns"), ConnectionError("dns"), "ok", "ok", ConnectionError("dns")])

    @jobs.job("flaky_test_job")
    def flaky(conn, **_):
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    for _ in range(6):
        try:
            flaky()
        except ConnectionError:
            pass
    assert [s.split(" ")[0] for s in sent] == ["⚠️", "✅", "⚠️"]
    jobs.JOBS.pop("flaky_test_job")
