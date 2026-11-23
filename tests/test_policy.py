import time

import pytest
from fastapi.testclient import TestClient

from app import config, policy
from app.main import app

FAST = 1000  # 75 simulated seconds -> 0.075 real seconds


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(config, "TIME_SPEEDUP", FAST)
    with TestClient(app) as c:
        c.post("/api/reset")
        yield c


def _events(client, type_=None, order_id="R-1002"):
    events = [e for e in client.get("/api/events?limit=200").json() if e["order_id"] == order_id]
    return [e for e in events if e["type"] == type_] if type_ else events


def _not_answered(client, result, order_id="R-1002"):
    return client.post(f"/api/orders/{order_id}/call-not-answered", json={"result": result})


def _wait_for_retry():
    time.sleep(0.4)


@pytest.mark.parametrize("outcome, pickup_calls, expected", [
    ("resolved", 1, (False, None)),
    ("no_answer", 1, (True, 75)),
    ("busy", 1, (True, 120)),
    ("declined", 1, (True, None)),
    ("voicemail", 1, (True, None)),
    ("partial", 1, (True, None)),
    ("no_answer", 2, (True, None)),
    ("busy", 2, (True, None)),
])
def test_next_step_by_bucket(outcome, pickup_calls, expected):
    assert policy.next_step(outcome, pickup_calls) == expected


def test_no_answer_texts_and_retries_once(client):
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, "no_answer")
    assert len(_events(client, "sms_sent")) == 1
    assert _events(client, "retry_scheduled")[0]["payload"]["in_seconds"] == 75
    _wait_for_retry()
    triggers = [e["payload"]["trigger"] for e in _events(client, "call_requested")]
    assert sorted(triggers) == ["pickup", "retry"]

    _not_answered(client, "no_answer")
    _wait_for_retry()
    assert len(_events(client, "call_requested")) == 2, "max 2 calls at pickup"
    assert len(_events(client, "sms_sent")) == 1, "text is sent only once"


@pytest.mark.parametrize("result", ["declined", "voicemail"])
def test_decline_and_voicemail_text_without_retry(client, result):
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, result)
    _wait_for_retry()
    assert len(_events(client, "sms_sent")) == 1
    assert not _events(client, "retry_scheduled")
    assert len(_events(client, "call_requested")) == 1


def test_text_reply_cancels_retry_and_confirms(client, monkeypatch):
    monkeypatch.setattr(config, "TIME_SPEEDUP", 1)
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, "busy")
    assert policy.is_pending("R-1002")
    client.post("/api/orders/R-1002/sms-reply", json={"text": "Building C, unit 204, leave at door"})
    assert not policy.is_pending("R-1002")
    assert _events(client, "retry_cancelled")[0]["payload"]["reason"] == "Customer replied by text"
    order = next(o for o in client.get("/api/orders").json() if o["id"] == "R-1002")
    assert order["handoff_status"] == "confirmed"
    assert order["decision_source"] == "sms"
    assert order["handoff_decision"]["instructions"] == "Building C, unit 204, leave at door"


def test_text_reply_for_alcohol_order_is_hand_to_customer(client):
    client.post("/api/orders/R-1006/pickup")
    client.post("/api/orders/R-1006/sms-reply", json={"text": "Leave it on the porch"})
    order = next(o for o in client.get("/api/orders").json() if o["id"] == "R-1006")
    assert order["handoff_decision"]["handoff_method"] == "hand_to_customer"


def test_arrive_cancels_retry_and_marks_unconfirmed(client, monkeypatch):
    monkeypatch.setattr(config, "TIME_SPEEDUP", 1)
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, "no_answer")
    client.post("/api/orders/R-1002/arrive")
    assert not policy.is_pending("R-1002")
    order = next(o for o in client.get("/api/orders").json() if o["id"] == "R-1002")
    assert order["handoff_status"] == "unconfirmed"


def test_unanswered_stall_call_does_nothing_more(client):
    client.post("/api/orders/R-1002/pickup")
    client.post("/api/orders/R-1002/arrive")
    client.post("/api/orders/R-1002/simulate-stall")
    _not_answered(client, "no_answer")
    _wait_for_retry()
    assert not _events(client, "retry_scheduled")
    assert len(_events(client, "call_requested")) == 2


def test_three_call_cap_includes_stall(client):
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, "no_answer")
    _wait_for_retry()
    _not_answered(client, "no_answer")
    assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is True
    assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is False
    assert len(_events(client, "call_requested")) == 3


def test_order_timeline_events(client):
    client.post("/api/orders/R-1002/pickup")
    _not_answered(client, "declined")
    client.post("/api/orders/R-1002/sms-reply", json={"text": "Unit 204"})
    types = [e["type"] for e in client.get("/api/orders/R-1002/events").json()]
    assert types[:3] == ["status_change", "rule_evaluated", "call_requested"]
    assert types[-1] == "decision_written"
    assert client.get("/api/orders/R-9999/events").status_code == 404


def test_metrics(client):
    for order_id in ("R-1002", "R-1003", "R-1004", "R-1005", "R-1001"):
        client.post(f"/api/orders/{order_id}/pickup")
    client.post("/api/orders/R-1005/sms-reply", json={"text": "Side gate, unit 4B"})
    _not_answered(client, "declined", "R-1003")
    client.post("/api/orders/R-1003/sms-reply", json={"text": "Code 4417"})
    client.post("/api/orders/R-1004/arrive")
    m = client.get("/api/metrics").json()
    assert m["picked_up"] == 5
    assert m["call_worthy_rate"] == 0.6
    assert m["resolution_rate"] == 0.33
    assert m["fallback_rate"] == 0.67
    assert m["resolved_before_arrival"] == 1.0
    assert m["resolved_by"] == {"Text": 2, "Never": 1, "In progress": 1}
