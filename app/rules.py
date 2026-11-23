import yaml

from app.config import ROOT

RULES_PATH = ROOT / "rules.yaml"
MULTI_UNIT = {"apartment", "complex"}


def load_rules() -> dict:
    return yaml.safe_load(RULES_PATH.read_text())


def evaluate(order: dict, history: dict, rules: dict | None = None) -> dict:
    rules = rules or load_rules()
    weights = rules["weights"]
    note = (order.get("dropoff_note") or "").strip()
    vague = any(k in note.lower() for k in rules["vague_keywords"])
    multi_unit = order["address_type"] in MULTI_UNIT
    signals: list[tuple[str, int]] = []

    def hit(key: str, label: str) -> None:
        signals.append((label, weights[key]))

    if multi_unit and not order.get("unit"):
        hit("multi_unit_no_unit", "Apartment or complex with no unit number")
    if order["address_type"] == "gated":
        hit("gated", "Gated community")
    if history["failed_count"] > 0:
        hit("past_failed_handoff", "Past failed handoff at this address")
    if vague:
        hit("vague_note", f'Vague drop-off note ("{note}")')
    elif not note:
        if multi_unit:
            hit("vague_note", "No drop-off note for an apartment or complex")
        else:
            hit("no_note", "No drop-off note")
    if history["success_count"] == 0:
        hit("first_delivery", "First delivery to this address")
    if order["dropoff_pref"] == "hand_to_me":
        hit("hand_to_me", '"Hand it to me" preference')
    if order["contains_alcohol"]:
        hit("contains_alcohol", "Contains alcohol (ID check required)")
    if (
        order["address_type"] == "house"
        and order["dropoff_pref"] == "leave_at_door"
        and note
        and not vague
        and history["success_count"] > 0
        and history["failed_count"] == 0
    ):
        hit("clear_house_dropoff", "House with a clear leave-at-door note and past successful drop-offs")

    score = sum(w for _, w in signals)
    reasons = [f"{label} ({w:+d})" for label, w in signals]

    override = _override(order, rules["overrides"])
    if override:
        action, why = override
        reasons.insert(0, f"Override: {why}")
    else:
        t = rules["thresholds"]
        action = "call" if score >= t["call"] else "sms" if score >= t["sms"] else "none"

    return {"action": action, "score": score, "reasons": reasons}


def _override(order: dict, overrides: dict) -> tuple[str, str] | None:
    if order.get("handoff_status") == "confirmed":
        return "none", "Handoff already confirmed"
    if order["call_opt_out"]:
        return "sms", "Customer prefers text"
    if order["contains_alcohol"] and overrides["alcohol_forces_call"]:
        return "call", "Alcohol order needs a live ID-check handoff"
    return None
