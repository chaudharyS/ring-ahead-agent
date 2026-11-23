# Ring Ahead Agent

A voice agent built on [Vapi](https://vapi.ai) that sorts out delivery drop-off details with the customer **while the courier is still driving**, so the courier never has to text at the wheel or wait at the door.

The demo runs on "Relay", a fictional delivery platform. All data is made up.

> Work in progress. Full write-up, architecture, and demo video coming soon.

## Run locally

Requires Python 3.11+ and [ngrok](https://ngrok.com).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your Vapi keys
uvicorn app.main:app --reload --port 8000 --timeout-graceful-shutdown 2
```

The shutdown timeout keeps `--reload` from hanging on the page's open live-update connection.

In another terminal, expose the server so Vapi can reach the webhook:

```bash
ngrok http 8000
```

Put the ngrok URL in `PUBLIC_BASE_URL` in `.env`.
