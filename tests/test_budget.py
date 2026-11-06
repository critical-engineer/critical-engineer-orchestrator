from datetime import datetime, timezone

import pytest

from ceo.budget import BudgetState, cap_from_env, check_budget, month_spend
from ceo.ledger import open_ledger

NOV = datetime(2026, 11, 15, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 1, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path):
    c = open_ledger(tmp_path / "ledger.db")
    c.execute("INSERT INTO plans (repo, change) VALUES ('r', 'c')")
    c.execute("INSERT INTO tasks (plan_id, id) VALUES (1, '1.1')")
    c.commit()
    yield c
    c.close()


def run(conn, cost, started="2026-11-10T10:00:00.000Z"):
    conn.execute(
        "INSERT INTO runs (plan_id, task_id, worker, started_at, cost_usd) VALUES (1, '1.1', 'opencode', ?, ?)",
        (started, cost),
    )
    conn.commit()


def alerts(conn):
    return [r[0] for r in conn.execute(
        "SELECT json_extract(detail, '$.threshold') FROM events WHERE kind = 'budget.alert'"
        " AND json_extract(detail, '$.delivered') = 1 ORDER BY id")]


def test_month_spend_counts_this_month_only(conn):
    run(conn, 1.5)
    run(conn, 2.25)
    run(conn, None)                                    # cost unknown: counts as 0
    run(conn, 100.0, started="2026-10-31T23:59:59.000Z")  # last month
    assert month_spend(conn, "2026-11") == pytest.approx(3.75)
    assert month_spend(conn, "2026-12") == 0


def test_under_budget_is_allowed_without_alerts(conn):
    run(conn, 10.0)
    state = check_budget(conn, 20.0, now=NOV)
    assert state.allowed and state.fraction == pytest.approx(0.5)
    assert alerts(conn) == []


def test_alerts_once_at_80_and_100_percent(conn):
    sent = []
    run(conn, 16.0)
    assert check_budget(conn, 20.0, alert=sent.append, now=NOV).allowed
    check_budget(conn, 20.0, alert=sent.append, now=NOV)  # no repeat
    run(conn, 4.0)
    state = check_budget(conn, 20.0, alert=sent.append, now=NOV)
    assert not state.allowed
    check_budget(conn, 20.0, alert=sent.append, now=NOV)
    assert alerts(conn) == [0.8, 1.0]
    assert len(sent) == 2 and "passed 80%" in sent[0] and "paused" in sent[1]


def test_jumping_past_both_thresholds_alerts_both(conn):
    sent = []
    run(conn, 25.0)
    check_budget(conn, 20.0, alert=sent.append, now=NOV)
    assert alerts(conn) == [0.8, 1.0] and len(sent) == 2


def test_a_new_month_starts_fresh(conn):
    run(conn, 25.0)
    check_budget(conn, 20.0, now=NOV)
    state = check_budget(conn, 20.0, now=DEC)
    assert state.allowed and state.spent_usd == 0
    run(conn, 17.0, started="2026-12-02T09:00:00.000Z")
    check_budget(conn, 20.0, now=DEC)
    assert alerts(conn) == [0.8, 1.0, 0.8]


def test_zero_cap_allows_nothing():
    assert not BudgetState("2026-11", 0.0, 0.0).allowed


def test_cap_from_env():
    assert cap_from_env({}) == 20.0
    assert cap_from_env({"CEO_BUDGET_USD": "5.5"}) == 5.5
    for bad in ("-1", "inf", "Infinity", "nan", "-inf"):
        with pytest.raises(ValueError, match="finite"):
            cap_from_env({"CEO_BUDGET_USD": bad})


def test_month_is_taken_in_utc():
    # 23:30 on 31 October in New York is already November in UTC.
    from datetime import timedelta
    new_york = timezone(timedelta(hours=-4))
    late_october_local = datetime(2026, 10, 31, 23, 30, tzinfo=new_york)
    from ceo.budget import check_budget as cb
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE runs (started_at TEXT, cost_usd REAL)")
    conn.execute("CREATE TABLE events (kind TEXT, plan_id, task_id, detail TEXT)")
    assert cb(conn, 20.0, now=late_october_local).month == "2026-11"


def test_a_failed_alert_is_retried_and_not_duplicated_once_sent(conn):
    run(conn, 17.0)
    calls = []

    def flaky(text):
        calls.append(text)
        if len(calls) == 1:
            raise RuntimeError("chat is down")

    check_budget(conn, 20.0, alert=flaky, now=NOV)   # fails: not marked sent
    assert alerts(conn) == []
    check_budget(conn, 20.0, alert=flaky, now=NOV)   # retried and delivered
    check_budget(conn, 20.0, alert=flaky, now=NOV)   # not sent again
    assert len(calls) == 2 and alerts(conn) == [0.8]
