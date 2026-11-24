import re

import httpx

from app import config

API = "https://api.vapi.ai"


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.VAPI_API_KEY}"}


def known_issue(reasons: list[str]) -> str:
    issues = []
    for r in reasons:
        if re.search(r"\(-\d+\)$", r):
            continue
        issues.append(re.sub(r" \([+-]\d+\)$", "", r.removeprefix("Override: ")))
    issues = [i[0].lower() + i[1:] for i in issues[:2]]
    return " and ".join(issues) or "confirming the drop-off details"


def first_question(order: dict) -> str:
    reasons = " ".join(order["rule_reasons"]).lower()
    if order["contains_alcohol"]:
        return ("Since your order includes alcohol, the delivery partner has to hand it to an adult with ID. "
                "Will you be able to meet them?")
    if "no unit number" in reasons:
        return "What's your building and unit number?"
    if order["address_type"] == "gated":
        return "Is there a gate code the delivery partner will need?"
    if "vague drop-off note" in reasons:
        return "Where exactly should the delivery partner drop it off?"
    if "past failed handoff" in reasons:
        return "Where exactly should the delivery partner leave it so it doesn't get missed?"
    return "How would you like the delivery partner to hand it off?"


def status_line(order: dict, trigger: str) -> str:
    if trigger == "arrival_stall":
        return f"Your delivery partner is outside with your {order['restaurant']} order now."
    return f"Your {order['restaurant']} order is about {order['eta_minutes']} minutes away."


def call_variables(order: dict, trigger: str) -> dict:
    return {
        "order_id": order["id"],
        "customer_first_name": order["customer_name"].split()[0],
        "status_line": status_line(order, trigger),
        "first_question": first_question(order),
        "restaurant": order["restaurant"],
        "street_name": re.sub(r"^\d+\s+", "", order["address_line"]),
        "eta_minutes": str(order["eta_minutes"]),
        "known_issue": known_issue(order["rule_reasons"]),
        "dropoff_pref": order["dropoff_pref"].replace("_", " "),
        "contains_alcohol": "true" if order["contains_alcohol"] else "false",
        "trigger": trigger,
    }


def start_phone_call(order: dict, variables: dict, metadata: dict) -> str:
    body = {
        "assistantId": config.VAPI_ASSISTANT_ID,
        "phoneNumberId": config.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": order["customer_phone"]},
        "assistantOverrides": {"variableValues": variables, "metadata": metadata},
    }
    r = httpx.post(f"{API}/call", json=body, headers=_headers(), timeout=15)
    r.raise_for_status()
    return r.json()["id"]


def create_webhook_credential(secret: str) -> str:
    body = {
        "provider": "custom-credential",
        "name": "ring-ahead-webhook",
        "authenticationPlan": {"type": "bearer", "token": secret, "headerName": "Authorization",
                               "bearerPrefixEnabled": True},
    }
    r = httpx.post(f"{API}/credential", json=body, headers=_headers(), timeout=20)
    if r.is_error:
        raise SystemExit(f"Vapi returned {r.status_code} creating the credential: {r.text}")
    return r.json()["id"]


def upsert_assistant(payload: dict, assistant_id: str | None) -> dict:
    if assistant_id:
        r = httpx.patch(f"{API}/assistant/{assistant_id}", json=payload, headers=_headers(), timeout=20)
    else:
        r = httpx.post(f"{API}/assistant", json=payload, headers=_headers(), timeout=20)
    if r.is_error:
        raise SystemExit(f"Vapi returned {r.status_code}: {r.text}")
    return r.json()
