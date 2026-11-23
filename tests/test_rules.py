import pytest
from fastapi.testclient import TestClient

from app import rules, seed
from app.main import app


def _order(o: dict) -> dict:
    return {"contains_alcohol": False, "call_opt_out": False, **o}


def _history(o: dict) -> dict:
    return {"success_count": o["history"][0], "failed_count": o["history"][1]}


@pytest.mark.parametrize("o", seed.SEED, ids=[o["id"] for o in seed.SEED])
def test_seed_orders_match_expected_action(o):
    result = rules.evaluate(_order(o), _history(o))
    assert result["action"] == o["expected"], result["reasons"]


def test_reasons_show_weights():
    o = next(x for x in seed.SEED if x["id"] == "R-1002")
    result = rules.evaluate(_order(o), _history(o))
    assert result["score"] == 6
    assert "Apartment or complex with no unit number (+3)" in result["reasons"]


def test_alcohol_override_beats_clear_house_discount():
    o = next(x for x in seed.SEED if x["id"] == "R-1006")
    result = rules.evaluate(_order(o), _history(o))
    assert result["score"] < 3
    assert result["reasons"][0].startswith("Override: Alcohol")


def test_pickup_all_through_api():
    with TestClient(app) as client:
        client.post("/api/reset")
        for o in seed.SEED:
            assert client.post(f"/api/orders/{o['id']}/pickup").status_code == 200
        orders = {o["id"]: o for o in client.get("/api/orders").json()}
        for o in seed.SEED:
            assert orders[o["id"]]["rule_action"] == o["expected"]
        assert orders["R-1012"]["handoff_status"] == "confirmed"
        assert orders["R-1001"]["handoff_status"] == "not_needed"
        assert orders["R-1002"]["handoff_status"] == "pending"
        assert client.post("/api/orders/R-1002/pickup").status_code == 409
        assert client.get("/api/metrics").json()["actions"] == {"call": 6, "sms": 3, "none": 3}


def test_arrive_marks_pending_handoff_unconfirmed():
    with TestClient(app) as client:
        client.post("/api/reset")
        for order_id in ("R-1002", "R-1001"):
            client.post(f"/api/orders/{order_id}/pickup")
            client.post(f"/api/orders/{order_id}/arrive")
        orders = {o["id"]: o for o in client.get("/api/orders").json()}
        assert orders["R-1002"]["handoff_status"] == "unconfirmed"
        assert orders["R-1002"]["decision_source"] == "existing_note"
        assert orders["R-1001"]["handoff_status"] == "not_needed"
        assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is True


def test_stall_respects_call_cap():
    with TestClient(app) as client:
        client.post("/api/reset")
        client.post("/api/orders/R-1002/pickup")
        assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is True
        assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is True
        assert client.post("/api/orders/R-1002/simulate-stall").json()["called"] is False
        assert client.post("/api/orders/R-1012/pickup").status_code == 200
        assert client.post("/api/orders/R-1012/simulate-stall").json()["called"] is False
