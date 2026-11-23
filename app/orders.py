import json
from collections import Counter

from app import config, rules, seed
from app.db import Session

MAX_CALLS_PER_ORDER = 3


def _to_dict(row) -> dict:
    d = dict(row)
    d["contains_alcohol"] = bool(d["contains_alcohol"])
    d["call_opt_out"] = bool(d["call_opt_out"])
    d["rule_reasons"] = json.loads(d["rule_reasons"]) if d["rule_reasons"] else []
    d["handoff_decision"] = json.loads(d["handoff_decision"]) if d["handoff_decision"] else None
    d["expected_action"] = seed.EXPECTED.get(d["id"])
    return d


def list_orders(s: Session) -> list[dict]:
    rows = s.conn.execute("SELECT * FROM orders ORDER BY id").fetchall()
    return [_to_dict(r) for r in rows]


def get_order(s: Session, order_id: str) -> dict | None:
    row = s.conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return _to_dict(row) if row else None


def _history(s: Session, address_line: str) -> dict:
    row = s.conn.execute("SELECT * FROM address_history WHERE address_line = ?", (address_line,)).fetchone()
    return dict(row) if row else {"success_count": 0, "failed_count": 0}


def _set(s: Session, order_id: str, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    s.conn.execute(f"UPDATE orders SET {cols} WHERE id = ?", (*fields.values(), order_id))


def _set_status(s: Session, order: dict, status: str) -> None:
    _set(s, order["id"], status=status)
    s.log_event(order["id"], "status_change", {"from": order["status"], "to": status})
    order["status"] = status


def run_rules(s: Session, order: dict) -> dict:
    result = rules.evaluate(order, _history(s, order["address_line"]))
    _set(s, order["id"], rule_action=result["action"], rule_score=result["score"],
         rule_reasons=json.dumps(result["reasons"]))
    s.log_event(order["id"], "rule_evaluated", result)
    return result


def call_attempts(s: Session, order_id: str) -> int:
    row = s.conn.execute(
        "SELECT COUNT(*) FROM events WHERE order_id = ? AND type = 'call_requested'", (order_id,)
    ).fetchone()
    return row[0]


def request_call(s: Session, order: dict, trigger: str) -> None:
    # Day 2 replaces this with a real Vapi call (web or phone, per CALL_MODE).
    s.log_event(order["id"], "call_requested",
                {"trigger": trigger, "attempt": call_attempts(s, order["id"]) + 1, "mode": config.CALL_MODE})


def send_sms(s: Session, order: dict) -> None:
    text = (f"Relay: your order is ~{order['eta_minutes']} min away. "
            "Reply with your building/unit or how to drop off.")
    s.log_event(order["id"], "sms_sent", {"text": text})


def pickup(s: Session, order: dict) -> dict:
    _set_status(s, order, "picked_up")
    result = run_rules(s, order)
    if order["handoff_status"] == "confirmed":
        return result
    if result["action"] == "none":
        _set(s, order["id"], handoff_status="not_needed")
        return result
    _set(s, order["id"], handoff_status="pending")
    if result["action"] == "call":
        request_call(s, order, trigger="pickup")
    else:
        send_sms(s, order)
    return result


def arrive(s: Session, order: dict) -> None:
    _set_status(s, order, "arrived")
    if order["handoff_status"] == "pending":
        _set(s, order["id"], handoff_status="unconfirmed", decision_source="existing_note")
        order["handoff_status"] = "unconfirmed"
        s.log_event(order["id"], "fallback", {"reason": "Courier arrived with no confirmed handoff; use the note"})


def simulate_stall(s: Session, order: dict) -> dict:
    if order["status"] != "arrived":
        _set_status(s, order, "arrived")
    if order["handoff_status"] == "confirmed":
        return {"called": False, "reason": "Handoff already confirmed"}
    if call_attempts(s, order["id"]) >= MAX_CALLS_PER_ORDER:
        return {"called": False, "reason": f"Reached the {MAX_CALLS_PER_ORDER}-call limit for this order"}
    s.log_event(order["id"], "arrival_stall", {"stopped_seconds": 120})
    request_call(s, order, trigger="arrival_stall")
    return {"called": True, "reason": "Stopped at the address for 120 s with no confirmed handoff"}


def recent_events(s: Session, limit: int) -> list[dict]:
    rows = s.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]


def metrics(s: Session) -> dict:
    rows = s.conn.execute("SELECT rule_action, handoff_status FROM orders WHERE status != 'placed'").fetchall()
    n = len(rows)
    actions = Counter(r["rule_action"] for r in rows)
    return {
        "picked_up": n,
        "call_worthy_rate": round(actions["call"] / n, 2) if n else None,
        "actions": {a: actions[a] for a in ("call", "sms", "none")},
        "handoff": dict(Counter(r["handoff_status"] for r in rows)),
    }
