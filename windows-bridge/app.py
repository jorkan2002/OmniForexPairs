import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager

import MetaTrader5 as mt5
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from analysis import TIMEFRAME_MAP, full_analysis
from account import dashboard_data
import trading

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mt5-bridge")

MT5_LOGIN = int(os.environ["MT5_LOGIN"])
MT5_PASSWORD = os.environ["MT5_PASSWORD"]
MT5_SERVER = os.environ["MT5_SERVER"]
MT5_SYMBOLS = [s.strip() for s in os.environ["MT5_SYMBOLS"].split(",") if s.strip()]
MT5_TERMINAL_PATH = os.environ.get(
    "MT5_TERMINAL_PATH", r"C:\Program Files\Mond Trades MT5 Terminal\terminal64.exe"
)
TRADE_LOT_SIZE = float(os.environ.get("TRADE_LOT_SIZE", "0.01"))

POLL_INTERVAL_SECONDS = 1.0
RECONNECT_BACKOFF_SECONDS = 10
TRADING_CYCLE_SECONDS = 20

trading.trading_state["lot_size"] = TRADE_LOT_SIZE

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


async def trading_loop():
    while True:
        await asyncio.sleep(TRADING_CYCLE_SECONDS)
        if not state["connected"]:
            continue
        try:
            await asyncio.to_thread(trading.run_cycle, MT5_SYMBOLS)
        except Exception:
            log.exception("trading cycle failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    price_task = asyncio.create_task(price_loop())
    trade_task = asyncio.create_task(trading_loop())
    yield
    price_task.cancel()
    trade_task.cancel()
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


@app.get("/api/dashboard")
async def get_dashboard():
    if not state["connected"]:
        raise HTTPException(status_code=503, detail="not connected to MT5")
    return await asyncio.to_thread(dashboard_data)


class TradingToggle(BaseModel):
    enabled: bool


class TradingConfig(BaseModel):
    lot_size: float


@app.get("/api/trading/status")
async def get_trading_status():
    ti = mt5.terminal_info() if state["connected"] else None
    return {
        **trading.trading_state,
        "symbols": MT5_SYMBOLS,
        "terminal_autotrading_allowed": bool(ti.trade_allowed) if ti else None,
    }


@app.post("/api/trading/toggle")
async def set_trading_toggle(body: TradingToggle):
    trading.trading_state["enabled"] = body.enabled
    trading.trading_state["last_error"] = None
    log.info("Auto-trading %s", "ENABLED" if body.enabled else "disabled")
    return {"enabled": trading.trading_state["enabled"]}


@app.post("/api/trading/config")
async def set_trading_config(body: TradingConfig):
    if body.lot_size <= 0:
        raise HTTPException(status_code=400, detail="lot_size must be positive")
    trading.trading_state["lot_size"] = round(body.lot_size, 2)
    log.info("Trading lot size set to %s", trading.trading_state["lot_size"])
    return {"lot_size": trading.trading_state["lot_size"]}


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
