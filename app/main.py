import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse

from app import bus, config, db, orders, seed


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    with db.session() as s:
        if not s.conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone():
            seed.reset(s)
    yield


app = FastAPI(title="Ring Ahead Agent", lifespan=lifespan)

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
        return orders.list_orders(s)


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
        orders.arrive(s, order)
        return {"ok": True}


@app.post("/api/orders/{order_id}/simulate-stall")
async def simulate_stall(order_id: str):
    with db.session() as s:
        order = _require(s, order_id)
        if order["status"] not in ("picked_up", "arrived"):
            raise HTTPException(409, f"Order {order_id} is {order['status']}; pick it up first")
        return orders.simulate_stall(s, order)


@app.post("/api/orders/{order_id}/rerun-rules")
async def rerun_rules(order_id: str):
    with db.session() as s:
        return orders.run_rules(s, _require(s, order_id))


@app.post("/api/reset")
async def reset():
    with db.session() as s:
        seed.reset(s)
    return {"ok": True}


@app.get("/api/metrics")
async def metrics():
    with db.session() as s:
        return orders.metrics(s)


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
