import pytest
from fastapi.testclient import TestClient

from app import vapi
from app.main import app

AUTH = {"Authorization": "Bearer test-secret"}


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


def _orders(client):
    return {o["id"]: o for o in client.get("/api/orders").json()}


def _tool_call(client, args, headers=AUTH):
    body = {"message": {
        "type": "tool-calls",
        "call": {"id": "call-1"},
        "toolCallList": [{"id": "tc-1", "type": "function",
                          "function": {"name": "submit_handoff_decision", "arguments": args}}],
    }}
    return client.post("/vapi/webhook", json=body, headers=headers)


def test_rejects_missing_or_wrong_secret(client):
    assert _tool_call(client, {}, headers={}).status_code == 401
    assert _tool_call(client, {}, headers={"Authorization": "Bearer nope"}).status_code == 401


def test_tool_call_writes_decision(client):
    client.post("/api/orders/R-1002/pickup")
    res = _tool_call(client, {"order_id": "R-1002", "building": "C", "unit": "204", "entry_code": "4417",
                              "handoff_method": "leave_at_door", "instructions": "x" * 200,
                              "customer_confirmed": True})
    assert res.status_code == 200
    assert res.json()["results"][0]["toolCallId"] == "tc-1"
    assert "result" in res.json()["results"][0]
    order = _orders(client)["R-1002"]
    assert order["handoff_status"] == "confirmed"
    assert order["decision_source"] == "call"
    assert order["handoff_decision"]["unit"] == "204"
    assert len(order["handoff_decision"]["instructions"]) == 120


def test_arguments_as_json_string(client):
    res = _tool_call(client, '{"order_id": "R-1002", "handoff_method": "meet_outside", "customer_confirmed": true}')
    assert "result" in res.json()["results"][0]


def test_alcohol_cannot_be_left_at_door(client):
    res = _tool_call(client, {"order_id": "R-1006", "handoff_method": "leave_at_door", "customer_confirmed": True})
    assert "alcohol" in res.json()["results"][0]["error"]
    assert _orders(client)["R-1006"]["handoff_status"] != "confirmed"
    res = _tool_call(client, {"order_id": "R-1006", "handoff_method": "hand_to_customer", "customer_confirmed": True})
    assert "result" in res.json()["results"][0]


def test_unconfirmed_tool_call_is_rejected(client):
    res = _tool_call(client, {"order_id": "R-1002", "handoff_method": "meet_outside", "customer_confirmed": False})
    assert "error" in res.json()["results"][0]


def _end_of_call(client, call_id, reason):
    body = {"message": {"type": "end-of-call-report", "endedReason": reason, "call": {"id": call_id},
                        "startedAt": "2026-11-23T10:00:00.000Z", "endedAt": "2026-11-23T10:00:38.500Z"}}
    return client.post("/vapi/webhook", json=body, headers=AUTH)


def test_unresolved_call_falls_back_to_text(client):
    client.post("/api/orders/R-1002/pickup")
    client.post("/api/orders/R-1002/web-call-started", json={"call_id": "call-9", "trigger": "pickup", "attempt": 1})
    assert _end_of_call(client, "call-9", "customer-ended-call").status_code == 200
    events = client.get("/api/events?limit=20").json()
    ended = next(e for e in events if e["type"] == "call_ended")
    assert ended["payload"]["outcome"] == "partial"
    assert ended["payload"]["duration_s"] == 38.5
    assert sum(e["type"] == "sms_sent" for e in events) == 1


def test_resolved_call_sends_no_text(client):
    client.post("/api/orders/R-1002/pickup")
    client.post("/api/orders/R-1002/web-call-started", json={"call_id": "call-9", "trigger": "pickup", "attempt": 1})
    _tool_call(client, {"order_id": "R-1002", "handoff_method": "meet_outside", "customer_confirmed": True})
    _end_of_call(client, "call-9", "assistant-ended-call")
    events = client.get("/api/events?limit=20").json()
    assert next(e for e in events if e["type"] == "call_ended")["payload"]["outcome"] == "resolved"
    assert not any(e["type"] == "sms_sent" for e in events)


def test_decline_sends_text_once(client):
    client.post("/api/orders/R-1002/pickup")
    client.post("/api/orders/R-1002/call-not-answered", json={"result": "declined"})
    client.post("/api/orders/R-1002/call-not-answered", json={"result": "declined"})
    events = client.get("/api/events?limit=20").json()
    assert sum(e["type"] == "sms_sent" for e in events) == 1


def test_call_requested_carries_variables(client):
    client.post("/api/orders/R-1002/pickup")
    req = next(e for e in client.get("/api/events").json() if e["type"] == "call_requested")
    v = req["payload"]["variables"]
    assert v["street_name"] == "Maple Street"
    assert v["customer_first_name"] == "Priya"
    assert v["known_issue"] == "apartment or complex with no unit number and no drop-off note for an apartment or complex"


def test_first_question_matches_the_issue(client):
    for order_id in ("R-1002", "R-1003", "R-1006"):
        client.post(f"/api/orders/{order_id}/pickup")
    events = client.get("/api/events?limit=50").json()
    q = {e["order_id"]: e["payload"]["variables"]["first_question"] for e in events if e["type"] == "call_requested"}
    assert q["R-1002"] == "What's your building and unit number?"
    assert "gate code" in q["R-1003"]
    assert "alcohol" in q["R-1006"]


def test_stall_call_says_courier_is_outside(client):
    client.post("/api/orders/R-1002/pickup")
    client.post("/api/orders/R-1002/simulate-stall")
    events = client.get("/api/events?limit=50").json()
    stall = next(e for e in events if e["type"] == "call_requested" and e["payload"]["trigger"] == "arrival_stall")
    assert "outside" in stall["payload"]["variables"]["status_line"]


def test_duplicate_save_is_ignored(client):
    args = {"order_id": "R-1002", "unit": "204", "handoff_method": "leave_at_door", "customer_confirmed": True}
    _tool_call(client, args)
    assert "Already saved" in _tool_call(client, args).json()["results"][0]["result"]
    events = client.get("/api/events?limit=50").json()
    assert sum(e["type"] == "decision_written" for e in events) == 1
    _tool_call(client, {**args, "unit": "205"})
    assert _orders(client)["R-1002"]["handoff_decision"]["unit"] == "205"


def test_known_issue_skips_negative_and_override_prefix():
    reasons = ["Override: Alcohol order needs a live ID-check handoff", "Contains alcohol (ID check required) (+2)",
               "House with a clear leave-at-door note and past successful drop-offs (-4)"]
    assert vapi.known_issue(reasons) == "alcohol order needs a live ID-check handoff and contains alcohol (ID check required)"
