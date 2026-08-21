import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from analysis import TIMEFRAME_MAP, full_analysis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mt5-bridge")

MT5_LOGIN = int(os.environ["MT5_LOGIN"])
MT5_PASSWORD = os.environ["MT5_PASSWORD"]
MT5_SERVER = os.environ["MT5_SERVER"]
MT5_SYMBOLS = [s.strip() for s in os.environ["MT5_SYMBOLS"].split(",") if s.strip()]
MT5_TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_PATH", r"C:\Program Files\Mond Trades MT5 Terminal\terminal64.exe"
)

POLL_INTERVAL_SECONDS = 1.0
RECONNECT_BACKOFF_SECONDS = 10

state = {
    "connected": False,
    "last_error": None,
    "symbols": {
        symbol: {"symbol": symbol, "bid": None, "ask": None, "time": None} for symbol in MT5_SYMBOLS
    },
}

clients: set[WebSocket] = set()


async def broadcast(payload: dict):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


def connect_mt5():
    ok = mt5.initialize(
        path=MT5_TERMINAL_PATH,
        login=MT5_LOGIN,
        password=MT5_PASSWORD,
        server=MT5_SERVER,
    )
    if not ok:
        raise RuntimeError(f"mt5.initialize() failed: {mt5.last_error()}")
    for symbol in MT5_SYMBOLS:
        mt5.symbol_select(symbol, True)


def poll_ticks():
    updates = []
    for symbol in MT5_SYMBOLS:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            continue
        entry = state["symbols"][symbol]
        entry["bid"] = tick.bid
        entry["ask"] = tick.ask
        entry["time"] = tick.time
        updates.append(dict(entry))
    return updates


async def price_loop():
    connected = False
    while True:
        try:
            if not connected:
                log.info("Connecting to MT5 account %s on %s ...", MT5_LOGIN, MT5_SERVER)
                await asyncio.to_thread(connect_mt5)
                connected = True
                state["connected"] = True
                state["last_error"] = None
                log.info("Connected to MT5 account %s on %s", MT5_LOGIN, MT5_SERVER)

            updates = await asyncio.to_thread(poll_ticks)
            if not updates:
                raise RuntimeError(f"no ticks for any symbol: {mt5.last_error()}")

            state["connected"] = True
            state["last_error"] = None

            await broadcast({"type": "prices", "symbols": updates})
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        except Exception as exc:
            log.warning("MT5 connection error: %s", exc)
            state["connected"] = False
            state["last_error"] = str(exc)
            await broadcast({"type": "status", "connected": False, "error": str(exc)})
            connected = False
            mt5.shutdown()
            await asyncio.sleep(RECONNECT_BACKOFF_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(price_loop())
    yield
    task.cancel()
    mt5.shutdown()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/prices")
async def get_prices():
    return state


@app.get("/api/status")
async def get_status():
    return {"connected": state["connected"], "last_error": state["last_error"], "time": time.time()}


@app.get("/api/analysis/{symbol}")
async def get_analysis(symbol: str, tf: str = "5m"):
    if symbol not in MT5_SYMBOLS:
        raise HTTPException(status_code=404, detail=f"unknown symbol: {symbol}")
    if tf not in TIMEFRAME_MAP:
        raise HTTPException(status_code=400, detail=f"unknown timeframe: {tf}")
    return await asyncio.to_thread(full_analysis, symbol, tf)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    clients.add(websocket)
    await websocket.send_json({"type": "prices", "symbols": list(state["symbols"].values())})
    if not state["connected"]:
        await websocket.send_json({"type": "status", "connected": False, "error": state["last_error"]})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        clients.discard(websocket)
