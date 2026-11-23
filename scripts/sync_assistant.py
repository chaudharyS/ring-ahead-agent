"""Create or update the Vapi assistant from app/assistant_config.py.

Creates the webhook Bearer credential first if .env doesn't have one yet.
Run: .venv/bin/python -m scripts.sync_assistant
"""
import re

from app import assistant_config, config, vapi

ENV_PATH = config.ROOT / ".env"


def save_env(key: str, value: str) -> None:
    text = ENV_PATH.read_text()
    if re.search(rf"^{key}=", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text)


def main() -> None:
    missing = [k for k in ("VAPI_API_KEY", "PUBLIC_BASE_URL", "VAPI_WEBHOOK_SECRET") if not getattr(config, k)]
    if missing:
        raise SystemExit(f"Set these in .env first: {', '.join(missing)}")

    if not config.VAPI_SERVER_CREDENTIAL_ID:
        config.VAPI_SERVER_CREDENTIAL_ID = vapi.create_webhook_credential(config.VAPI_WEBHOOK_SECRET)
        save_env("VAPI_SERVER_CREDENTIAL_ID", config.VAPI_SERVER_CREDENTIAL_ID)
        print(f"Created webhook credential {config.VAPI_SERVER_CREDENTIAL_ID} and saved it to .env")

    existing = config.VAPI_ASSISTANT_ID or None
    assistant = vapi.upsert_assistant(assistant_config.build(), existing)
    if existing:
        print(f"Updated assistant {assistant['id']}")
    else:
        save_env("VAPI_ASSISTANT_ID", assistant["id"])
        print(f"Created assistant {assistant['id']} and saved it to .env as VAPI_ASSISTANT_ID")
    print(f"Webhook: {assistant['server']['url']}")


if __name__ == "__main__":
    main()
