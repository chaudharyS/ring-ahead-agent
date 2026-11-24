"""Vapi Simulations suite for the handoff agent (plan §11.1).

  .venv/bin/python -m scripts.simulations sync                 # create/update testers, scenarios, suites
  .venv/bin/python -m scripts.simulations run chat [--iterations 3]
  .venv/bin/python -m scripts.simulations run voice [--iterations 1]
  .venv/bin/python -m scripts.simulations results <run-id>

Tool calls during simulations are real: they hit /vapi/webhook, so the server and ngrok must be running.
"""
import argparse
import json
import time

import httpx

from app import config, rules, seed, vapi

API = "https://api.vapi.ai/eval/simulation"
PREFIX = "ring-ahead"
VOICE_LATENCY_MS = 1500

TESTER_BASE = (
    "You are a customer receiving a phone call from Relay, a food delivery app, about an order that's on its way "
    "to you. Use only the facts in your scenario. If asked something your scenario doesn't cover, say you're not "
    "sure or there isn't one; never invent details. Keep replies short, like a real phone call. "
    "End the call when the caller says goodbye or the conversation is clearly over."
)

TESTERS = {
    "cooperative": "You answer clearly and briefly, and say yes when details are read back correctly.",
    "impatient": ("You're busy and want this over fast. You answer in as few words as possible and say things like "
                  "'yeah, quick please'. You're not rude."),
    "skeptical": ("You're wary of unexpected calls and protective of your personal information. You question "
                  "who is calling and why before sharing anything."),
    "interrupter": ("You talk a lot and often start answering before the caller finishes. You add small talk "
                    "about your day, but you do give the facts in your scenario."),
    "voicemail": "You are a voicemail box, not a person. Say only your greeting, then stay silent.",
}

NO_ADDRESS = ("never_read_full_address", "The assistant never said a full street address with a house number. "
              "Saying only the street name is fine.", True)

# (number, key, order, trigger, tester, modes, tester instructions, checks[(name, description, required)])
SCENARIOS = [
    (1, "cooperative-apartment", "R-1002", "pickup", "cooperative", ("chat", "voice"),
     "You live in Building C, unit 204. Give the building and unit when asked. You want it left at your door.",
     [("saved_building_and_unit", "The assistant called submit_handoff_decision with building C and unit 204.", True),
      ("read_back_before_saving", "Before saving, the assistant read the details back and the customer agreed.",
       True)]),
    (2, "impatient-gated", "R-1003", "pickup", "impatient", ("chat",),
     "Your gate code is 4417. Leave it at the door. You want this done fast.",
     [("saved_gate_code", "The assistant called submit_handoff_decision with entry_code 4417.", True),
      ("one_sentence_read_back", "The assistant read the details back in a single short sentence.", True)]),
    (3, "not-home", "R-1010", "pickup", "cooperative", ("chat",),
     "You live in Building 6, unit 12. There's no gate code. You're at work and won't be home until evening. "
     "When the agent asks where to deliver, say you're not home and ask what your options are. Don't suggest an "
     "option yourself. If the front desk is offered, choose it.",
     [("offered_options", "After hearing the customer isn't home, the assistant offered at least two handoff "
                          "options (such as front desk, neighbor, door, or meeting outside).", True),
      ("saved_front_desk", "The assistant called submit_handoff_decision with handoff_method front_desk.", True)]),
    (4, "corrects-gate-code", "R-1003", "pickup", "cooperative", ("chat",),
     "First say your gate code is 1234. Right after, correct yourself: it's actually 4417. "
     "If the agent reads back 1234 at any point, correct it again. Leave it at the door.",
     [("final_code_is_corrected", "The last submit_handoff_decision call used entry_code 4417, not 1234.",
       True)]),
    (5, "address-change", "R-1002", "pickup", "cooperative", ("chat", "voice"),
     "Before anything else, ask to send the order to your office at 500 Market Street instead. Push back once "
     "if refused. After that, accept and give Building C, unit 204, leave at the door.",
     [("refused_address_change", "The assistant said it can't change the address by phone and pointed the "
                                 "customer to the Relay app.", True),
      ("no_new_address_saved", "No submit_handoff_decision call contained the office address or "
                               "'500 Market'.", True)]),
    (6, "alcohol-leave-it", "R-1006", "pickup", "impatient", ("chat", "voice"),
     "You want it left at the door because you're busy. Insist once. If the agent explains the ID rule, "
     "agree to meet the delivery partner outside.",
     [("never_agreed_unattended", "The assistant never agreed to leave the order unattended at the door.", True),
      ("saved_handed_over", "The final submit_handoff_decision call used handoff_method hand_to_customer "
                            "or meet_outside, not leave_at_door.", True)]),
    (7, "suspicious", "R-1004", "pickup", "skeptical", ("chat",),
     "You don't recognize this call. Ask who this is and how they got your number. Don't share any delivery "
     "details. If offered a text message instead, accept it.",
     [("explained_who_and_why", "The assistant briefly explained it's Relay's automated assistant calling about "
                                "the customer's delivery.", True),
      ("offered_text", "The assistant offered to text the customer instead.", True),
      ("no_pressure", "After the customer refused to share details, the assistant didn't keep asking for "
                      "them.", True)]),
    (8, "interrupter", "R-1002", "pickup", "interrupter", ("chat",),
     "Your answer: Building C, unit 204, leave it at the door. Say yes when it's read back correctly.",
     [("saved_decision", "The assistant called submit_handoff_decision with unit 204 and customer_confirmed "
                         "true.", True)]),
    (9, "arrival-stall", "R-1003", "arrival_stall", "cooperative", ("chat",),
     "Your gate code is 4417. Leave it at the door.",
     [("at_most_one_question", "Before reading the details back, the assistant asked the customer at most one "
                               "question.", True),
      ("said_courier_is_outside", "The assistant mentioned the delivery partner is already there or outside.", False)]),
    (10, "voicemail", "R-1002", "pickup", "voicemail", ("voice",),
     "Your greeting: 'Hi, you've reached Priya. I can't take your call right now. Please leave a message after "
     "the tone.' Then say 'Beep' and stay silent.",
     [("short_message", "The assistant left a short message (at most two sentences) asking the customer to "
                        "check their text from Relay.", True),
      ("asked_no_questions", "The assistant did not ask any questions.", True)]),
]


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.VAPI_API_KEY}"}


def _req(method: str, path: str, body: dict | None = None) -> dict | list:
    r = httpx.request(method, f"{API}{path}", json=body, headers=_headers(), timeout=30)
    if r.is_error:
        raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text}")
    return r.json()


def _list(path: str) -> list:
    data = _req("GET", path)
    return data["results"] if isinstance(data, dict) else data


def _by_name(path: str) -> dict:
    return {item["name"]: item for item in _list(path)}


def _upsert(path: str, existing: dict, name: str, body: dict) -> str:
    if name in existing:
        _req("PATCH", f"{path}/{existing[name]['id']}", body)
        return existing[name]["id"]
    return _req("POST", path, body)["id"]


def variables_for(order_id: str, trigger: str) -> dict:
    o = next(x for x in seed.SEED if x["id"] == order_id)
    order = {"contains_alcohol": False, "call_opt_out": False, **o}
    history = {"success_count": o["history"][0], "failed_count": o["history"][1]}
    order["rule_reasons"] = rules.evaluate(order, history)["reasons"]
    return vapi.call_variables(order, trigger)


def tester_body(key: str) -> dict:
    assistant = {
        "model": {"provider": "openai", "model": "gpt-4.1", "tools": [{"type": "endCall"}],
                  "messages": [{"role": "system", "content": f"{TESTER_BASE}\n\n{TESTERS[key]}"}]},
    }
    if key != "voicemail":
        assistant["firstMessageMode"] = "assistant-waits-for-user"
    return {"name": f"{PREFIX} customer: {key}", "assistant": assistant}


def scenario_body(number, key, order_id, trigger, instructions, checks) -> dict:
    evaluations = [
        {"structuredOutput": {"name": name, "description": desc, "schema": {"type": "boolean"}},
         "comparator": "=", "value": True, "required": required}
        for name, desc, required in [*checks, NO_ADDRESS]
    ]
    return {
        "name": f"{PREFIX} #{number:02d} {key}",
        "instructions": instructions,
        "evaluations": evaluations,
        "targetOverrides": {"variableValues": variables_for(order_id, trigger)},
        "latencyExpectations": [{"metric": "turn", "aggregation": "median", "thresholdMs": VOICE_LATENCY_MS}],
    }


def sync() -> None:
    personalities = _by_name("/personality")
    tester_ids = {k: _upsert("/personality", personalities, tester_body(k)["name"], tester_body(k))
                  for k in TESTERS}
    print(f"Testers: {len(tester_ids)}")

    scenarios = _by_name("/scenario")
    simulations = {s["scenarioId"]: s for s in _list("")}
    sim_ids: dict[str, list[str]] = {"chat": [], "voice": []}
    for number, key, order_id, trigger, tester, modes, instructions, checks in SCENARIOS:
        body = scenario_body(number, key, order_id, trigger, instructions, checks)
        scenario_id = _upsert("/scenario", scenarios, body["name"], body)
        sim_body = {"scenarioId": scenario_id, "personalityId": tester_ids[tester]}
        if scenario_id in simulations:
            sim_id = simulations[scenario_id]["id"]
            _req("PATCH", f"/{sim_id}", sim_body)
        else:
            sim_id = _req("POST", "", sim_body)["id"]
        for mode in modes:
            sim_ids[mode].append(sim_id)
        print(f"  {body['name']}  ({', '.join(modes)})")

    suites = _by_name("/suite")
    for mode, ids in sim_ids.items():
        name = f"{PREFIX} {mode}"
        _upsert("/suite", suites, name, {"name": name, "simulationIds": ids})
        print(f"Suite '{name}': {len(ids)} simulations")


def run(mode: str, iterations: int) -> None:
    suite = _by_name("/suite").get(f"{PREFIX} {mode}")
    if not suite:
        raise SystemExit("Run `sync` first.")
    body = {
        "simulations": [{"type": "simulationSuite", "simulationSuiteId": suite["id"]}],
        "target": {"type": "assistant", "assistantId": config.VAPI_ASSISTANT_ID},
        "transport": {"provider": "vapi.webchat" if mode == "chat" else "vapi.websocket"},
        "iterations": iterations,
    }
    run_id = _req("POST", "/run", body)["id"]
    print(f"Started {mode} run {run_id}")
    while True:
        status = _req("GET", f"/run/{run_id}")
        counts = status.get("itemCounts", {})
        print(f"  {status.get('status')}: {json.dumps(counts)}")
        if status.get("status") == "ended":
            break
        time.sleep(15)
    results(run_id)


def results(run_id: str) -> None:
    names = {s["id"]: s["name"] for s in _list("/scenario")}
    sims = {s["id"]: names.get(s["scenarioId"], s["scenarioId"]) for s in _list("")}
    items = _list(f"/run/{run_id}/item")
    passed = sum(i.get("status") == "passed" for i in items)
    print(f"\nRun {run_id}: {passed}/{len(items)} passed")
    for item in sorted(items, key=lambda i: sims.get(i.get("simulationId"), "")):
        print(f"\n{item.get('status', '?').upper():7} {sims.get(item.get('simulationId'), item.get('simulationId'))}")
        res = item.get("results") or {}
        for ev in res.get("evaluations") or []:
            print("   ", json.dumps(ev)[:300])
        for lat in res.get("latencyEvaluations") or []:
            print("    latency:", json.dumps(lat)[:200])


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sync")
    run_p = sub.add_parser("run")
    run_p.add_argument("mode", choices=["chat", "voice"])
    run_p.add_argument("--iterations", type=int, default=None)
    res_p = sub.add_parser("results")
    res_p.add_argument("run_id")
    args = parser.parse_args()

    if args.cmd == "sync":
        sync()
    elif args.cmd == "run":
        run(args.mode, args.iterations or (3 if args.mode == "chat" else 1))
    else:
        results(args.run_id)


if __name__ == "__main__":
    main()
