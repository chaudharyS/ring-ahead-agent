import json

from app import config
from app.db import Session, now

# Each order covers one branch of the rules. "expected" is the action the plan expects.
SEED = [
    dict(id="R-1001", customer_name="Maya Chen", restaurant="Golden Thai", address_line="14 Birch Lane",
         address_type="house", unit=None, dropoff_note="Leave at the front door, thanks!", dropoff_pref="leave_at_door",
         eta_minutes=12, history=(5, 0), expected="none"),
    dict(id="R-1002", customer_name="Priya Raman", restaurant="Saffron House", address_line="220 Maple Street",
         address_type="apartment", unit=None, dropoff_note="", dropoff_pref="leave_at_door",
         eta_minutes=15, history=(0, 0), expected="call"),
    dict(id="R-1003", customer_name="Daniel Okafor", restaurant="Burger Barn", address_line="8 Willow Creek Drive",
         address_type="gated", unit=None, dropoff_note="Gate at the entrance", dropoff_pref="leave_at_door",
         eta_minutes=10, history=(1, 0), expected="call"),
    dict(id="R-1004", customer_name="Lucas Moreno", restaurant="Pizza Nova", address_line="57 Cedar Court",
         address_type="house", unit=None, dropoff_note="Leave on the porch", dropoff_pref="leave_at_door",
         eta_minutes=14, history=(3, 2), expected="call"),
    dict(id="R-1005", customer_name="Aisha Khan", restaurant="Green Bowl", address_line="301 Elm Avenue",
         address_type="apartment", unit="4B", dropoff_note="side entrance?", dropoff_pref="leave_at_door",
         eta_minutes=11, history=(2, 0), expected="sms"),
    dict(id="R-1006", customer_name="Tom Becker", restaurant="Corner Market", address_line="9 Aspen Road",
         address_type="house", unit=None, dropoff_note="Leave at door", dropoff_pref="leave_at_door",
         contains_alcohol=True, eta_minutes=13, history=(2, 0), expected="call"),
    dict(id="R-1007", customer_name="Sofia Rossi", restaurant="Noodle Bar", address_line="480 Pine Street",
         address_type="apartment", unit=None, dropoff_note="", dropoff_pref="leave_at_door",
         call_opt_out=True, eta_minutes=16, history=(0, 0), expected="sms"),
    dict(id="R-1008", customer_name="James Park", restaurant="Taco Spot", address_line="12 Oak Hollow Way",
         address_type="gated", unit=None, dropoff_note="", dropoff_pref="leave_at_door",
         eta_minutes=2, history=(1, 0), expected="call"),
    dict(id="R-1009", customer_name="Nina Patel", restaurant="Sushi Go", address_line="100 Harbor Plaza",
         address_type="office", unit="Suite 300", dropoff_note="Leave with the front desk", dropoff_pref="leave_at_door",
         eta_minutes=9, history=(0, 0), expected="none"),
    dict(id="R-1010", customer_name="Omar Haddad", restaurant="Spice Route", address_line="600 Lakeside Commons",
         address_type="complex", unit=None, dropoff_note="", dropoff_pref="hand_to_me",
         eta_minutes=12, history=(1, 0), expected="call"),
    dict(id="R-1011", customer_name="Emily Brooks", restaurant="Bagel Co", address_line="33 Spruce Street",
         address_type="house", unit=None, dropoff_note="", dropoff_pref="leave_at_door",
         eta_minutes=10, history=(0, 0), expected="sms"),
    dict(id="R-1012", customer_name="Ken Watanabe", restaurant="Golden Thai", address_line="75 Chestnut Street",
         address_type="apartment", unit="12", dropoff_note="Buzz 12", dropoff_pref="hand_to_me",
         eta_minutes=7, history=(4, 0), expected="none", handoff_status="confirmed", decision_source="call",
         handoff_decision={"unit": "12", "handoff_method": "hand_to_customer",
                           "instructions": "Buzz 12, customer comes down", "customer_confirmed": True}),
]

EXPECTED = {o["id"]: o["expected"] for o in SEED}


def reset(s: Session) -> None:
    for table in ("orders", "address_history", "calls", "events"):
        s.conn.execute(f"DELETE FROM {table}")
    ts = now()
    for o in SEED:
        success, failed = o["history"]
        s.conn.execute(
            "INSERT INTO address_history (address_line, success_count, failed_count) VALUES (?, ?, ?)",
            (o["address_line"], success, failed),
        )
        decision = o.get("handoff_decision")
        s.conn.execute(
            """INSERT INTO orders (id, customer_name, customer_phone, restaurant, address_line, address_type, unit,
                 dropoff_note, dropoff_pref, contains_alcohol, call_opt_out, status, eta_minutes,
                 handoff_status, handoff_decision, decision_source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'placed', ?, ?, ?, ?, ?)""",
            (o["id"], o["customer_name"], config.DEMO_CUSTOMER_PHONE, o["restaurant"], o["address_line"],
             o["address_type"], o["unit"], o["dropoff_note"], o["dropoff_pref"], int(o.get("contains_alcohol", False)),
             int(o.get("call_opt_out", False)), o["eta_minutes"], o.get("handoff_status"),
             json.dumps(decision) if decision else None, o.get("decision_source"), ts),
        )
    s.log_event(None, "reset")
