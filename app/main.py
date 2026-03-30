from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection

# Routers
from app.api.v1 import auth, broker, trades
from app.websockets.stream import manager  # <-- WebSocket manager import kiya

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    await connect_to_mongo()

@app.on_event("shutdown")
async def shutdown_event():
    await close_mongo_connection()

# --- ROUTERS ATTACH KAREIN ---
app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(broker.router, prefix=f"{settings.API_V1_STR}/broker", tags=["Broker Integration"])
app.include_router(trades.router, prefix=f"{settings.API_V1_STR}/trades", tags=["Trade Execution"])

# --- WEBSOCKET ROUTE ---
@app.websocket("/ws/live-data/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    await manager.connect(websocket, user_id)
    try:
        while True:
            # Frontend se agar koi ping/pong message aaye usko handle karne ke liye
            data = await websocket.receive_text()
            # Abhi ke liye hum sirf connection zinda rakh rahe hain
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)

@app.get("/")
async def root():
    return {
        "platform": settings.PROJECT_NAME, 
        "status": "Online",
        "websockets": "Active"
    }
