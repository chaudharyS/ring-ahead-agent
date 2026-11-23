from fastapi import FastAPI
from fastapi.responses import FileResponse

from app import config

app = FastAPI(title="Ring Ahead Agent")


@app.get("/")
def index():
    return FileResponse(config.ROOT / "static" / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "call_mode": config.CALL_MODE}
