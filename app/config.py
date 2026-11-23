import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

VAPI_API_KEY = os.getenv("VAPI_API_KEY", "")
VAPI_PUBLIC_KEY = os.getenv("VAPI_PUBLIC_KEY", "")
VAPI_ASSISTANT_ID = os.getenv("VAPI_ASSISTANT_ID", "")
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID", "")
CALL_MODE = os.getenv("CALL_MODE", "web")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "")
TIME_SPEEDUP = float(os.getenv("TIME_SPEEDUP", "1"))
DATABASE_PATH = ROOT / os.getenv("DATABASE_PATH", "relay.db")
DEMO_CUSTOMER_PHONE = os.getenv("DEMO_CUSTOMER_PHONE", "")
