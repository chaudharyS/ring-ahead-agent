# Ring Ahead Agent

A voice agent built on [Vapi](https://vapi.ai) that sorts out delivery drop-off details with the customer **while the courier is still driving**, so the courier never has to text at the wheel or wait at the door.

The demo runs on "Relay", a fictional delivery platform. All data is made up.

## The problem

Drop-off questions (which building, what's the gate code, leave it or hand it over) surface when the courier arrives, at the worst possible moment. The courier is driving or standing at the door, texting a customer who isn't looking at their phone.

> The handoff question is asked at the wrong time (arrival), by the wrong person (a driving courier), through the wrong channel (a text nobody sees).

## How it works

1. The courier marks an order **picked up**.
2. Explainable **"worth a call?" rules** decide: `call`, `sms`, or `none`, and list every reason.
3. On `call`, the Vapi agent phones the customer right away and asks only what it needs.
4. It reads the details back in one sentence, then **saves them through a tool call while the call is still live**.
5. The courier's screen flips to **Handoff confirmed** before they arrive. Hands stay on the wheel.

If the customer doesn't pick up, the next step depends on *why*, and the courier always ends up with something to act on.

## The rules: not every order needs a call

Weights live in [`rules.yaml`](rules.yaml) and can be tuned live from the ops screen.

| Signal | Weight |
|---|---|
| Apartment or complex with no unit number | +3 |
| Gated community | +3 |
| Past failed handoff at this address | +3 |
| Vague drop-off note ("side entrance?", or empty at an apartment) | +2 |
| Contains alcohol (ID check) | +2 |
| First delivery to this address | +1 |
| No drop-off note (house, office, gated) | +1 |
| "Hand it to me" preference | +1 |
| House with a clear leave-at-door note, past successes, no failures | −4 |

Score ≥ 3 → call, 2 → text, ≤ 1 → nothing. Overrides run first: already confirmed → nothing; customer opted out of calls → text; alcohol → always call, because an ID check needs a live handoff.

## The agent

Defined in code ([`app/assistant_config.py`](app/assistant_config.py)) and synced to Vapi with one script.

- **Opens with the real question** for that order ("What's your building and unit number?", "Is there a gate code?"), chosen from the rule reasons.
- **Saves mid-call** with `submit_handoff_decision`. The backend validates every save and sends errors back so the agent corrects itself. Example: an alcohol order can't be saved as "leave at door".
- **Guardrails:** says it's automated, refuses address changes by phone (fraud risk) and points to the app, never reads out the full address, offers a text to wary customers.
- **Webhooks are authenticated** with a Vapi Bearer credential, so nobody else can write fake handoffs.

## When the customer doesn't answer

| How the call ended | Next step |
|---|---|
| No answer | Text now + one retry after 75 s |
| Busy | Text now + one retry after 2 min |
| Declined / voicemail / hung up undecided | Text only |
| Confirmed | Stop everything |

- Max 2 calls at pickup and 3 per order, including one last call if the courier is stuck at the door for 120 s.
- A text reply cancels the pending retry. So does the courier tapping **Arrive**, which marks the handoff "unconfirmed" so the courier uses the existing note.
- Both retry gaps are under 3 minutes on purpose: with **Allow Repeated Calls** on, an iPhone lets a second call from the same number through Do Not Disturb if it comes within 3 minutes ([Apple Support](https://support.apple.com/guide/iphone/set-do-not-disturb-iph5c3f5b77b/ios)).

## Testing

- **48 backend tests** (`pytest`): every rule branch, the webhook, validation, and each retry/fallback path with its timing, call limits and cancellation.
- **Vapi Simulations** ([`scripts/simulations.py`](scripts/simulations.py)): an AI tester plays the customer across 10 scenarios (cooperative, impatient, not home, self-correction, address-change request, alcohol, suspicious caller, talks over the agent, courier already outside, voicemail). The agent's tool calls hit the real webhook, and results are cross-checked against the database.
  - Chat mode: **27/27** (9 scenarios × 3 runs).
  - Voice mode: **3/4**, median response time 972–1,261 ms. The failure is real and instructive: Vapi's voicemail detection didn't trigger in simulation, and the prompt had no fallback, so the agent questioned a voicemail box for 60 s.

## Architecture

```
┌──────────────────────┐        ┌──────────────────────┐
│  Ops + courier UI    │◀──SSE──│  FastAPI + SQLite    │
│  (one HTML page)     │──REST─▶│  rules · retries ·   │
└──────────┬───────────┘        │  metrics · webhook   │
           │ Vapi Web SDK       └──────────▲───────────┘
           ▼ (browser call)                │ tool-calls, status,
┌──────────────────────┐                   │ end-of-call (via ngrok)
│  Vapi assistant      │───────────────────┘
│  gpt-4.1-mini · Clara│──── phone call ───▶ customer (phone mode)
└──────────────────────┘
```

Python 3.12, FastAPI, SQLite, vanilla JS, no build step.

```
app/        backend: rules, orders, retry policy, metrics, Vapi webhook
scripts/    sync_assistant.py (create/update the agent), simulations.py (test suite)
static/     the single-page ops + courier UI
tests/      pytest suite
rules.yaml  the "worth a call?" weights
```

## Run locally

Requires Python 3.11+, [ngrok](https://ngrok.com) and a Vapi account.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # fill in Vapi keys, ngrok URL, a webhook secret
```

```bash
ngrok http --url=<your-static-domain> 8000                        # terminal 1
uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 2   # terminal 2
python -m scripts.sync_assistant   # creates the webhook credential + assistant, saves IDs to .env
```

Open http://localhost:8000, click **Pick up** on an order the rules mark `call`, then **Answer** the incoming call in the browser. The shutdown timeout keeps `--reload` from hanging on the page's open live-update connection.

```bash
pytest                                       # backend tests
python -m scripts.simulations sync           # push the Simulations suite to Vapi
python -m scripts.simulations run chat       # or: run voice
```

**Call modes.** `CALL_MODE=web` (default) runs the call in the browser with the Vapi Web SDK: same assistant, same webhook, no phone number needed. `CALL_MODE=phone` places real outbound calls and needs an outbound-capable number in `VAPI_PHONE_NUMBER_ID`; it hasn't been tested end to end yet.

## Known limits

- A real phone network doesn't report "declined": it usually shows up as voicemail or busy. The policy fails safe (at worst one extra call), but telling them apart needs real-call testing.
- Texts are mocked (logged and shown in the UI), not sent.
- Real deployments need explicit customer consent for automated calls (e.g. TCPA in the US). This demo only calls numbers the builder owns.
