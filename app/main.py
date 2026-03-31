from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.engine.scheduler import start_scheduler

# ✅ Purane 'manager' ki jagah Naya Live P&L engine import kiya
from app.websockets.stream import stream_live_pnl

# 📦 Saare Routers Import Karein
from app.api.v1 import auth, broker, trades, market, strategy

# 🚀 FastAPI App Initialize
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# 🌐 CORS Middleware (Frontend se connect hone ke liye)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 🚦 LIFECYCLE EVENTS ---
@app.on_event("startup")
async def startup_event():
    await connect_to_mongo()  # Database connect karo
    start_scheduler()         # Strategy checker timer start karo

@app.on_event("shutdown")
async def shutdown_event():
    await close_mongo_connection()

# --- 🔗 ROUTERS ATTACH KAREIN ---
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(broker.router, prefix=f"{settings.API_V1_STR}/broker", tags=["Broker Integration"])
app.include_router(trades.router, prefix=f"{settings.API_V1_STR}/trades", tags=["Trade Execution"])
app.include_router(market.router, prefix=f"{settings.API_V1_STR}/market", tags=["Market Data"])
app.include_router(strategy.router, prefix=f"{settings.API_V1_STR}/strategy", tags=["Strategy Builder"])

# --- 📡 WEBSOCKET ROUTE (For Live Data) ---
# ✅ Route update kiya. Ab URL mein ID nahi, Token aayega security aur sahi connection ke liye.
@app.websocket("/ws/live-data")
async def websocket_endpoint(websocket: WebSocket, token: str):
    await websocket.accept()
    # P&L calculation ka saara load ab stream.py handle karega
    await stream_live_pnl(websocket, token)

# --- 🩺 HEALTH CHECK ROUTE ---
@app.get("/")
async def root():
    return {
        "platform": settings.PROJECT_NAME, 
        "status": "Online",
        "database": "Connected",
        "scheduler": "Running",
        "websockets": "Active"
    }
