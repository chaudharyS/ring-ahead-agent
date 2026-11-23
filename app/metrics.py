"""Demo metrics (plan §11.2), computed from orders, calls and events."""
import json
import statistics
from collections import Counter

from app.db import Session

RESOLVED_BY_TRIGGER = {"pickup": "Call 1", "retry": "Call 2", "arrival_stall": "Arrival stall"}


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 2) if whole else None


def _first_ts(s: Session, order_id: str, type_: str, where: str = "") -> str | None:
    row = s.conn.execute(
        f"SELECT ts FROM events WHERE order_id = ? AND type = ? {where} ORDER BY id LIMIT 1", (order_id, type_)
    ).fetchone()
    return row["ts"] if row else None


def _resolved_by(s: Session, order: dict) -> str:
    if order["handoff_status"] == "unconfirmed":
        return "Never"
    if order["handoff_status"] != "confirmed":
        return "In progress"
    if order["decision_source"] == "sms":
        return "Text"
    row = s.conn.execute(
        "SELECT payload FROM events WHERE order_id = ? AND type = 'decision_written' ORDER BY id DESC LIMIT 1",
        (order["id"],),
    ).fetchone()
    call_id = json.loads(row["payload"]).get("call_id") if row else None
    call = s.conn.execute("SELECT trigger FROM calls WHERE id = ?", (call_id,)).fetchone() if call_id else None
    return RESOLVED_BY_TRIGGER.get(call["trigger"], "Call") if call else "Call"


def compute(s: Session) -> dict:
    picked = [dict(r) for r in s.conn.execute("SELECT * FROM orders WHERE status != 'placed'")]
    actions = Counter(o["rule_action"] for o in picked)
    called = [o for o in picked if o["rule_action"] == "call"]
    contacted = [o for o in picked if o["rule_action"] in ("call", "sms")]

    confirmed = [o for o in contacted if o["handoff_status"] == "confirmed"]
    before_arrival = 0
    for o in confirmed:
        written = _first_ts(s, o["id"], "decision_written")
        arrived = _first_ts(s, o["id"], "status_change", "AND json_extract(payload, '$.to') = 'arrived'")
        if written and (not arrived or written <= arrived):
            before_arrival += 1

    durations = [r[0] for r in s.conn.execute("SELECT duration_s FROM calls WHERE duration_s IS NOT NULL")]
    fell_back = [o for o in called if o["decision_source"] == "sms" or o["handoff_status"] == "unconfirmed"]
    resolved_by = Counter(_resolved_by(s, o) for o in contacted)

    return {
        "picked_up": len(picked),
        "actions": {a: actions[a] for a in ("call", "sms", "none")},
        "call_worthy_rate": _rate(actions["call"], len(picked)),
        "resolution_rate": _rate(sum(o["handoff_status"] == "confirmed" for o in called), len(called)),
        "resolved_before_arrival": _rate(before_arrival, len(confirmed)),
        "median_call_s": round(statistics.median(durations), 1) if durations else None,
        "calls_measured": len(durations),
        "fallback_rate": _rate(len(fell_back), len(called)),
        "resolved_by": {k: resolved_by[k] for k in ("Call 1", "Call 2", "Text", "Arrival stall", "Call", "Never",
                                                     "In progress") if resolved_by[k]},
    }
