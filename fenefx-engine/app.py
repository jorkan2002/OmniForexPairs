import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import engine
import state as st
import telegram_post
from providers import list_providers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fenefx-app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    auto_task = asyncio.create_task(engine.automatic_loop())
    track_task = asyncio.create_task(engine.tracking_loop())
    yield
    auto_task.cancel()
    track_task.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


class ConfigUpdate(BaseModel):
    provider: str | None = None
    mode: str | None = None
    interval_seconds: int | None = None


@app.get("/api/fenefx/status")
async def get_status():
    return {
        "state": st.state,
        "progress": st.progress,
        "providers": list_providers(),
        "symbols": st.SYMBOLS,
        "telegram_enabled": telegram_post.telegram_enabled(),
        "open_signals": len([s for s in telegram_post.open_signals if not s["closed"]]),
    }


@app.post("/api/fenefx/config")
async def set_config(body: ConfigUpdate):
    if body.provider is not None:
        st.state["active_provider"] = body.provider
    if body.mode is not None:
        if body.mode not in ("manual", "automatic"):
            raise HTTPException(status_code=400, detail="mode must be 'manual' or 'automatic'")
        st.state["mode"] = body.mode
    if body.interval_seconds is not None:
        st.state["interval_seconds"] = max(60, body.interval_seconds)
    st.log_action(f"Config updated: provider={st.state['active_provider']}, mode={st.state['mode']}, "
                  f"interval={st.state['interval_seconds']}s")
    return {"state": st.state}


async def _run_and_store(symbol: str):
    result = await engine.run_analysis(symbol)
    st.state["last_result"] = result


@app.post("/api/fenefx/analyze/{symbol}")
async def trigger_analysis(symbol: str):
    if symbol not in st.SYMBOLS:
        raise HTTPException(status_code=404, detail=f"unknown symbol: {symbol}")
    if st.progress["busy"]:
        raise HTTPException(status_code=409, detail="another analysis is already running")
    asyncio.create_task(_run_and_store(symbol))
    return {"started": True, "symbol": symbol}
