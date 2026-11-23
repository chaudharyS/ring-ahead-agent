import asyncio
import json
from contextlib import asynccontextmanager

from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app import bus, config, db, metrics, orders, policy, seed, webhook


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    with db.session() as s:
        if not s.conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
            seed.reset(s)
    yield


app = FastAPI(title="Ring Ahead Agent", lifespan=lifespan)
app.include_router(webhook.router)

# Handlers are async (no awaits inside db sessions) so bus.publish runs on the event loop.


def _require(s: db.Session, order_id: str) -> dict:
    order = orders.get_order(s, order_id)
    if not order:
        raise HTTPException(404, f"Order {order_id} not found")
    return order


@app.get("/")
async def index():
    return FileResponse(config.ROOT / "static" / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "call_mode": config.CALL_MODE}


@app.get("/api/orders")
async def list_orders():
    with db.session() as s:
        return [{**o, "retry_pending": policy.is_pending(o["id"])} for o in orders.list_orders(s)]


@app.get("/api/orders/{order_id}/events")
async def order_events(order_id: str):
    with db.session() as s:
        _require(s, order_id)
        return orders.order_events(s, order_id)


@app.post("/api/orders/{order_id}/pickup")
async def pickup(order_id: str):
    with db.session() as s:
        order = _require(s, order_id)
        if order["status"] != "placed":
            raise HTTPException(409, f"Order {order_id} is already {order['status']}")
        return orders.pickup(s, order)


@app.post("/api/orders/{order_id}/arrive")
async def arrive(order_id: str):
    with db.session() as s:
        order = _require(s, order_id)
        if order["status"] != "picked_up":
            raise HTTPException(409, f"Order {order_id} is {order['status']}, not picked up")
        policy.on_arrive(s, order)
        return {"ok": True}


@app.post("/api/orders/{order_id}/simulate-stall")
async def simulate_stall(order_id: str):
    with db.session() as s:
        order = _require(s, order_id)
        if order["status"] not in ("picked_up", "arrived"):
            raise HTTPException(409, f"Order {order_id} is {order['status']}; pick it up first")
        return policy.on_stall(s, order)


class TextReply(BaseModel):
    text: str = Field(min_length=1, max_length=500)


@app.post("/api/orders/{order_id}/sms-reply")
async def sms_reply(order_id: str, body: TextReply):
    with db.session() as s:
        order = _require(s, order_id)
        if order["status"] == "placed":
            raise HTTPException(409, f"Order {order_id} hasn't been picked up yet")
        policy.on_text_reply(s, order, body.text)
    return {"ok": True}


@app.get("/api/client-config")
async def client_config():
    # The public key is designed for browsers; never expose VAPI_API_KEY here.
    return {"call_mode": config.CALL_MODE, "public_key": config.VAPI_PUBLIC_KEY,
            "assistant_id": config.VAPI_ASSISTANT_ID, "time_speedup": config.TIME_SPEEDUP}


class Settings(BaseModel):
    time_speedup: float = Field(ge=1, le=100)


@app.post("/api/settings")
async def update_settings(body: Settings):
    config.TIME_SPEEDUP = body.time_speedup
    return {"time_speedup": config.TIME_SPEEDUP}


class WebCallStarted(BaseModel):
    call_id: str
    trigger: str
    attempt: int


@app.post("/api/orders/{order_id}/web-call-started")
async def web_call_started(order_id: str, body: WebCallStarted):
    with db.session() as s:
        _require(s, order_id)
        orders.record_call(s, body.call_id, order_id, body.trigger, body.attempt)
        s.log_event(order_id, "call_started", {"call_id": body.call_id, "mode": "web"})
    return {"ok": True}


class NotAnswered(BaseModel):
    result: Literal["declined", "no_answer", "busy", "voicemail"]


@app.post("/api/orders/{order_id}/call-not-answered")
async def call_not_answered(order_id: str, body: NotAnswered):
    with db.session() as s:
        order = _require(s, order_id)
        orders.call_not_answered(s, order, body.result)
        policy.on_call_ended(s, order, body.result)
    return {"ok": True}


@app.post("/api/orders/{order_id}/rerun-rules")
async def rerun_rules(order_id: str):
    with db.session() as s:
        return orders.run_rules(s, _require(s, order_id))


@app.post("/api/reset")
async def reset():
    policy.cancel_all()
    with db.session() as s:
        seed.reset(s)
    return {"ok": True}


@app.get("/api/metrics")
async def get_metrics():
    with db.session() as s:
        return metrics.compute(s)


@app.get("/api/events")
async def events(limit: int = 50):
    with db.session() as s:
        return orders.recent_events(s, min(limit, 200))


@app.get("/api/stream")
async def stream():
    async def gen():
        q = bus.subscribe()
        try:
            yield ": connected\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
