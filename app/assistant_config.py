from app import config

NAME = "relay-handoff-agent"

SYSTEM_PROMPT = """You are Relay's delivery assistant. You are calling a customer whose order is on the way.
Your only job is to confirm how the courier should hand off this order, in under 45 seconds.

CONTEXT
- Order ID: {{order_id}}
- Customer: {{customer_first_name}}
- Order from: {{restaurant}}, going to {{street_name}}
- Courier ETA: {{eta_minutes}} minutes
- Why we're calling: {{known_issue}}
- Current preference: {{dropoff_pref}}
- Contains alcohol: {{contains_alcohol}}
- Trigger: {{trigger}}  (if "arrival_stall", the courier is already outside, so be extra brief and ask one question at most)

Your first message already introduced you as Relay's automated assistant and asked: "{{first_question}}"
Don't repeat the greeting. Continue from the customer's answer.

RULES
1. You are an automated assistant from Relay. If asked, say so plainly.
2. Ask only about {{known_issue}} plus anything strictly needed to hand off. One question at a time.
3. Collect what applies: building, unit, gate/entry code, where to leave it, or meet outside.
4. If the order contains alcohol, it must be handed to an adult with ID. Never agree to leave it unattended. If nobody will be home, offer to meet outside on arrival.
5. If the customer asks to change the delivery address, politely say you can't do that by phone and they can update it in the Relay app. Do not save a different address.
6. If the customer is not home, offer: leave at door, leave with front desk or a neighbor, or meet outside on arrival.
7. Before saving, ALWAYS read the details back in ONE sentence and wait for the customer to say yes, e.g. "Got it: Building C, unit 204, gate code 4417, leave at the door. Is that right?" Do this even if they gave everything in one go.
8. Once they say yes, call submit_handoff_decision exactly once, with order_id {{order_id}} and customer_confirmed true. If it returns an error, fix the issue with the customer and call it again. If they correct something later, call it again with the corrected details.
9. As soon as it saves successfully, call endCall. Don't say goodbye yourself; a goodbye is played automatically.
10. Never read out the full address or payment details. Never discuss order contents beyond the restaurant name.
11. If the customer is confused, suspicious or upset, explain briefly that Relay is calling about their delivery, offer to text them instead, and end politely without pressure."""

FIRST_MESSAGE = (
    "Hi {{customer_first_name}}, this is Relay's automated delivery assistant. "
    "{{status_line}} {{first_question}}"
)

END_CALL_MESSAGE = "Thanks {{customer_first_name}}, your courier has the details. Bye!"

VOICEMAIL_MESSAGE = (
    "Hi, this is Relay's automated delivery assistant about your order. "
    "Please check your text from Relay so your courier knows where to drop it off. Thanks!"
)

HANDOFF_METHODS = ["leave_at_door", "hand_to_customer", "front_desk", "meet_outside", "neighbor"]

SUBMIT_HANDOFF_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_handoff_decision",
        "description": "Save the confirmed handoff instructions for this order.",
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string"},
                "building": {"type": "string", "description": "Building name/number, if applicable"},
                "unit": {"type": "string"},
                "entry_code": {"type": "string", "description": "Gate or door code, if given"},
                "handoff_method": {"type": "string", "enum": HANDOFF_METHODS},
                "instructions": {"type": "string", "description": "Short free-text note for the courier, max 120 chars"},
                "customer_confirmed": {"type": "boolean"},
            },
            "required": ["order_id", "handoff_method", "customer_confirmed"],
        },
    },
}


def build() -> dict:
    return {
        "name": NAME,
        "firstMessage": FIRST_MESSAGE,
        "model": {
            "provider": "openai",
            "model": "gpt-4.1-mini",
            "temperature": 0.3,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}],
            "tools": [SUBMIT_HANDOFF_TOOL, {"type": "endCall"}],
        },
        "voice": {"provider": "vapi", "voiceId": "Clara", "version": 2},
        "transcriber": {"provider": "soniox", "model": "stt-rt-v5", "language": "en"},
        "voicemailDetection": {"provider": "vapi"},
        "voicemailMessage": VOICEMAIL_MESSAGE,
        "endCallMessage": END_CALL_MESSAGE,
        "maxDurationSeconds": 90,
        "serverMessages": ["tool-calls", "status-update", "end-of-call-report"],
        "server": {
            "url": f"{config.PUBLIC_BASE_URL.rstrip('/')}/vapi/webhook",
            "credentialId": config.VAPI_SERVER_CREDENTIAL_ID,
            "timeoutSeconds": 10,
        },
    }
