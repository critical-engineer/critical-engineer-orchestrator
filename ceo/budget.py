"""Budget enforcement (T12, G5): API model spend stays under a monthly cap.

Spend is the sum of ``runs.cost_usd`` for the current calendar month (UTC).
The engineer's own subscription isn't counted (G5, D12). Before each dispatch
CEO checks the budget: at the cap, dispatch pauses. The engineer is alerted
once per month at 80% and once at 100%.

A single run can overshoot the cap, because its cost is only known after it
finishes; the OpenRouter key's own credit limit is the hard backstop (C3).

Configuration (environment): CEO_BUDGET_USD (default 20).
"""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from ceo.ledger import record_event

ALERT_THRESHOLDS = (0.8, 1.0)


@dataclass(frozen=True)
class BudgetState:
    month: str  # "YYYY-MM"
    spent_usd: float
    cap_usd: float

    @property
    def allowed(self) -> bool:
        return self.spent_usd < self.cap_usd

    @property
    def fraction(self) -> float:
        return self.spent_usd / self.cap_usd if self.cap_usd > 0 else 1.0


def cap_from_env(env: dict[str, str] | None = None) -> float:
    env = os.environ if env is None else env
    cap = float(env.get("CEO_BUDGET_USD", "20"))
    if cap < 0:
        raise ValueError("CEO_BUDGET_USD can't be negative")
    return cap


def month_spend(conn: sqlite3.Connection, month: str) -> float:
    row = conn.execute(
        "SELECT coalesce(sum(cost_usd), 0) FROM runs WHERE substr(started_at, 1, 7) = ?", (month,)
    ).fetchone()
    return float(row[0])


def check_budget(
    conn: sqlite3.Connection,
    cap_usd: float,
    *,
    alert: Callable[[str], None] | None = None,
    now: datetime | None = None,
) -> BudgetState:
    """Current spend against the cap; alerts the engineer once per threshold per month."""
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    state = BudgetState(month, month_spend(conn, month), cap_usd)
    for threshold in ALERT_THRESHOLDS:
        if state.fraction < threshold:
            continue
        already = conn.execute(
            "SELECT 1 FROM events WHERE kind = 'budget.alert'"
            " AND json_extract(detail, '$.month') = ? AND json_extract(detail, '$.threshold') = ?",
            (month, threshold),
        ).fetchone()
        if already:
            continue
        record_event(
            conn,
            "budget.alert",
            detail={"month": month, "threshold": threshold, "spent_usd": state.spent_usd, "cap_usd": cap_usd},
        )
        if alert is not None:
            verb = "reached" if threshold >= 1.0 else f"passed {int(threshold * 100)}% of"
            pause = " Dispatch is paused until next month or a higher cap." if threshold >= 1.0 else ""
            alert(f"API spend ${state.spent_usd:.2f} has {verb} the ${cap_usd:.2f} monthly budget.{pause}")
    return state
