import os
import tempfile
from pathlib import Path

os.environ["DATABASE_PATH"] = str(Path(tempfile.mkdtemp()) / "test.db")
os.environ["CALL_MODE"] = "web"
os.environ["VAPI_WEBHOOK_SECRET"] = "test-secret"
