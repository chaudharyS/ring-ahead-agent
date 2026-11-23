import json
from collections import Counter

import httpx

from app import assistant_config, config, rules, seed, vapi
from app.db import Session, now

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
    order.update(rule_action=result["action"], rule_score=result["score"], rule_reasons=result["reasons"])
    return result


def _count_events(s: Session, order_id: str, type_: str) -> int:
    row = s.conn.execute("SELECT COUNT(*) FROM events WHERE order_id = ? AND type = ?", (order_id, type_)).fetchone()
    return row[0]


def call_attempts(s: Session, order_id: str) -> int:
    return _count_events(s, order_id, "call_requested")


def record_call(s: Session, call_id: str, order_id: str, trigger: str, attempt: int) -> None:
    s.conn.execute(
        """INSERT OR IGNORE INTO calls (id, order_id, trigger, attempt, status, started_at)
           VALUES (?, ?, ?, ?, 'queued', ?)""",
        (call_id, order_id, trigger, attempt, now()),
    )


def request_call(s: Session, order: dict, trigger: str) -> None:
    attempt = call_attempts(s, order["id"]) + 1
    variables = vapi.call_variables(order, trigger)
    payload = {"trigger": trigger, "attempt": attempt, "mode": config.CALL_MODE, "variables": variables}
    if config.CALL_MODE == "phone":
        try:
            call_id = vapi.start_phone_call(order, variables, {"order_id": order["id"], "trigger": trigger})
        except httpx.HTTPError as e:
            s.log_event(order["id"], "call_failed", {"error": str(e)})
            send_sms(s, order)
            return
        record_call(s, call_id, order["id"], trigger, attempt)
        payload["call_id"] = call_id
    # In web mode the browser shows an incoming-call banner and starts the call itself.
    s.log_event(order["id"], "call_requested", payload)


def send_sms(s: Session, order: dict) -> None:
    text = (f"Relay: your order is ~{order['eta_minutes']} min away. "
            "Reply with your building/unit or how to drop off.")
    s.log_event(order["id"], "sms_sent", {"text": text})


def submit_handoff(s: Session, args: dict, call_id: str | None) -> tuple[bool, str]:
    order = get_order(s, str(args.get("order_id", "")))
    if not order:
        return False, "Unknown order_id. Use the order ID from your instructions."
    if not args.get("customer_confirmed"):
        return False, "Confirm the details back to the customer first, then call again with customer_confirmed true."
    method = args.get("handoff_method")
    if method not in assistant_config.HANDOFF_METHODS:
        return False, f"handoff_method must be one of: {', '.join(assistant_config.HANDOFF_METHODS)}."
    if order["contains_alcohol"] and method == "leave_at_door":
        return False, ("This order contains alcohol, so it can't be left at the door. "
                       "It must be handed to an adult with ID. Ask how they'd like to receive it.")

    decision = {k: str(args[k]).strip() for k in ("building", "unit", "entry_code") if args.get(k)}
    decision["handoff_method"] = method
    if args.get("instructions"):
        decision["instructions"] = str(args["instructions"]).strip()[:120]
    decision["customer_confirmed"] = True

    if order["handoff_status"] == "confirmed" and order["handoff_decision"] == decision:
        return True, "Already saved. Call endCall now."
    _set(s, order["id"], handoff_status="confirmed", decision_source="call", handoff_decision=json.dumps(decision))
    s.log_event(order["id"], "decision_written", {"source": "call", "call_id": call_id, "decision": decision})
    return True, "Saved. Call endCall now; the goodbye plays automatically."


def update_call_status(s: Session, call_id: str, status: str) -> None:
    row = s.conn.execute("SELECT order_id FROM calls WHERE id = ?", (call_id,)).fetchone()
    if not row:
        return
    s.conn.execute("UPDATE calls SET status = ? WHERE id = ?", (status, call_id))
    s.log_event(row["order_id"], "call_status", {"call_id": call_id, "status": status})


def _outcome(ended_reason: str, confirmed: bool) -> str:
    if confirmed:
        return "resolved"
    return {"customer-did-not-answer": "no_answer", "customer-busy": "busy", "voicemail": "voicemail"}.get(
        ended_reason, "partial"
    )


def call_ended(s: Session, call_id: str, order_id: str, ended_reason: str, duration_s: float | None) -> None:
    order = get_order(s, order_id)
    if not order:
        return
    outcome = _outcome(ended_reason, order["handoff_status"] == "confirmed")
    s.conn.execute(
        "UPDATE calls SET status = 'ended', ended_reason = ?, duration_s = ?, outcome = ?, ended_at = ? WHERE id = ?",
        (ended_reason, duration_s, outcome, now(), call_id),
    )
    s.log_event(order_id, "call_ended",
                {"call_id": call_id, "ended_reason": ended_reason, "duration_s": duration_s, "outcome": outcome})
    if outcome != "resolved" and order["status"] != "arrived":
        fallback_to_text(s, order, reason=f"Call ended without a decision ({ended_reason})")


def fallback_to_text(s: Session, order: dict, reason: str) -> None:
    s.log_event(order["id"], "fallback", {"reason": reason})
    if not _count_events(s, order["id"], "sms_sent"):
        send_sms(s, order)


def decline_call(s: Session, order: dict) -> None:
    s.log_event(order["id"], "call_declined", {})
    fallback_to_text(s, order, reason="Customer declined the call")


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
