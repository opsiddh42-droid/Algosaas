from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.core.database import connect_to_mongo, close_mongo_connection

# Routers import karein
from app.api.v1 import auth
from app.api.v1 import broker 
from app.api.v1 import trades  # <-- Naya Trades route import kiya

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
app.include_router(trades.router, prefix=f"{settings.API_V1_STR}/trades", tags=["Trade Execution"]) # <-- Attach kiya

@app.get("/")
async def root():
    return {
        "platform": settings.PROJECT_NAME, 
        "status": "Online",
        "database": "Connected"
    }
