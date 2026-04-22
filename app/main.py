from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.engine.scheduler import start_scheduler

# ✅ Live P&L engine import
from app.websockets.stream import stream_live_pnl

# 📦 Saare Routers Import (Analysis aur algotwo yahan add kiya hai)
from app.api.v1 import auth, broker, trades, market, strategy, algo, Analysis, algotwo

# 🚀 FastAPI App Initialize
app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

# 🌐 CORS Middleware (Amplify URL ke sath)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:3000",
        "https://main.d1c2da5x9u63dd.amplifyapp.com" # 🟢 Aapka exact Amplify URL (bina last '/' ke)
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 🚦 LIFECYCLE EVENTS ---
@app.on_event("startup")
async def startup_event():
    await connect_to_mongo()  
    start_scheduler()         

@app.on_event("shutdown")
async def shutdown_event():
    await close_mongo_connection()

# --- 🔗 ROUTERS ATTACH KAREIN ---
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(broker.router, prefix=f"{settings.API_V1_STR}/broker", tags=["Broker Integration"])
app.include_router(trades.router, prefix=f"{settings.API_V1_STR}/trades", tags=["Trade Execution"])
app.include_router(market.router, prefix=f"{settings.API_V1_STR}/market", tags=["Market Data"])
app.include_router(strategy.router, prefix=f"{settings.API_V1_STR}/strategy", tags=["Strategy Builder"])
app.include_router(algo.router, prefix=f"{settings.API_V1_STR}/algo", tags=["Algo Bot"]) 
app.include_router(Analysis.router, prefix=f"{settings.API_V1_STR}/analysis", tags=["Market Analysis"])

# 👇 Naya Trend Algo (Algo Two) Router Yahan Attach Kar Diya 👇
app.include_router(algotwo.router, prefix=f"{settings.API_V1_STR}/algotwo", tags=["Trend Algo"])

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

# --- 📡 WEBSOCKET ROUTE (For Live Data) ---
@app.websocket("/ws/live-data")
async def websocket_endpoint(websocket: WebSocket, token: str, mode: str = "PAPER"):
    await websocket.accept()
    await stream_live_pnl(websocket, token, mode)
