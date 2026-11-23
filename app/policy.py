"""Retry and fallback policy (plan §9.1, with Arrive as the cutoff)."""
import asyncio

from app import config, db, orders
from app.db import Session

MAX_PICKUP_CALLS = 2
RETRY_DELAY_S = {"no_answer": 75, "busy": 120}

_pending: dict[str, asyncio.Task] = {}


def next_step(outcome: str, pickup_calls: int) -> tuple[bool, float | None]:
    """Return (send_text, retry_delay_seconds) for how a call ended."""
    if outcome == "resolved":
        return False, None
    if outcome in RETRY_DELAY_S and pickup_calls < MAX_PICKUP_CALLS:
        return True, RETRY_DELAY_S[outcome]
    return True, None


def is_pending(order_id: str) -> bool:
    return order_id in _pending


def cancel(s: Session, order_id: str, reason: str) -> None:
    task = _pending.pop(order_id, None)
    if task:
        task.cancel()
        s.log_event(order_id, "retry_cancelled", {"reason": reason})


def cancel_all() -> None:
    for task in _pending.values():
        task.cancel()
    _pending.clear()


def on_call_ended(s: Session, order: dict, outcome: str) -> None:
    if outcome == "resolved":
        cancel(s, order["id"], "Handoff confirmed")
        return
    if order["status"] == "arrived":
        return  # Arrive already marked it unconfirmed; the stall call was the last attempt.
    send_text, delay = next_step(outcome, orders.pickup_calls(s, order["id"]))
    if send_text:
        orders.fallback_to_text(s, order, reason=f"Call ended without a decision ({outcome.replace('_', ' ')})")
    if delay:
        _schedule_retry(s, order["id"], delay, outcome)


def _schedule_retry(s: Session, order_id: str, delay_s: float, outcome: str) -> None:
    cancel(s, order_id, "Rescheduled")
    s.log_event(order_id, "retry_scheduled", {"in_seconds": delay_s, "after": outcome,
                                              "time_speedup": config.TIME_SPEEDUP})
    task = asyncio.get_running_loop().create_task(_retry_later(order_id, delay_s / config.TIME_SPEEDUP))
    _pending[order_id] = task


async def _retry_later(order_id: str, real_delay_s: float) -> None:
    await asyncio.sleep(real_delay_s)
    _pending.pop(order_id, None)
    with db.session() as s:
        order = orders.get_order(s, order_id)
        if not order or order["handoff_status"] != "pending" or order["status"] != "picked_up":
            return
        if orders.call_attempts(s, order_id) >= orders.MAX_CALLS_PER_ORDER:
            return
        orders.request_call(s, order, trigger="retry")


def on_text_reply(s: Session, order: dict, text: str) -> None:
    cancel(s, order["id"], "Customer replied by text")
    orders.write_text_decision(s, order, text)


def on_arrive(s: Session, order: dict) -> None:
    cancel(s, order["id"], "Courier arrived")
    orders.arrive(s, order)


def on_stall(s: Session, order: dict) -> dict:
    cancel(s, order["id"], "Courier is at the address")
    return orders.simulate_stall(s, order)
