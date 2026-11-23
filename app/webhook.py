import hmac
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from app import config, db, orders

router = APIRouter()


def _authorized(request: Request) -> bool:
    if not config.VAPI_WEBHOOK_SECRET:
        return False
    expected = f"Bearer {config.VAPI_WEBHOOK_SECRET}"
    return hmac.compare_digest(request.headers.get("authorization", ""), expected)


def _call_context(msg: dict) -> dict:
    call = msg.get("call") or {}
    overrides = call.get("assistantOverrides") or {}
    ctx: dict = {}
    for source in (
        (msg.get("artifact") or {}).get("variableValues") or {},
        overrides.get("variableValues") or {},
        overrides.get("metadata") or {},
    ):
        ctx.update({k: v for k, v in source.items() if k in ("order_id", "trigger")})
    return ctx


def _order_id_for_call(s: db.Session, call_id: str, msg: dict) -> str | None:
    row = s.conn.execute("SELECT order_id FROM calls WHERE id = ?", (call_id,)).fetchone()
    return row["order_id"] if row else _call_context(msg).get("order_id")


def _duration(msg: dict) -> float | None:
    try:
        start = datetime.fromisoformat(msg["startedAt"])
        end = datetime.fromisoformat(msg["endedAt"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((end - start).total_seconds(), 1)


def _tool_calls(msg: dict, call_id: str | None) -> dict:
    results = []
    for tc in msg.get("toolCallList") or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            args = json.loads(args or "{}")
        if fn.get("name") != "submit_handoff_decision":
            results.append({"toolCallId": tc["id"], "error": f"Unknown tool {fn.get('name')}"})
            continue
        with db.session() as s:
            ok, text = orders.submit_handoff(s, args, call_id)
        results.append({"toolCallId": tc["id"], "result" if ok else "error": text})
    return {"results": results}


@router.post("/vapi/webhook")
async def vapi_webhook(request: Request):
    if not _authorized(request):
        raise HTTPException(401, "Unauthorized")
    msg = (await request.json()).get("message") or {}
    kind = msg.get("type")
    call_id = (msg.get("call") or {}).get("id")

    if kind == "tool-calls":
        return _tool_calls(msg, call_id)

    if kind == "status-update" and call_id:
        with db.session() as s:
            orders.update_call_status(s, call_id, msg.get("status", ""))

    elif kind == "end-of-call-report" and call_id:
        with db.session() as s:
            order_id = _order_id_for_call(s, call_id, msg)
            if order_id:
                ctx = _call_context(msg)
                orders.record_call(s, call_id, order_id, ctx.get("trigger", "pickup"),
                                   max(orders.call_attempts(s, order_id), 1))
                orders.call_ended(s, call_id, order_id, msg.get("endedReason", ""), _duration(msg))

    return {}
