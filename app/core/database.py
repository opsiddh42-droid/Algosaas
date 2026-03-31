from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db_instance = Database()

async def connect_to_mongo():
    try:
        logger.info("⏳ Connecting to MongoDB...")
        db_instance.client = AsyncIOMotorClient(settings.MONGO_URI)
        db_instance.db = db_instance.client["tradingbot"] # 🟢 Seedha tradingbot
        logger.info("✅ MongoDB Connected Successfully!")
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Failed: {e}")

# 👇 YEH FUNCTION HONA BOHOT ZAROORI HAI
async def close_mongo_connection():
    if db_instance.client:
        db_instance.client.close()
        logger.info("🛑 MongoDB Connection Closed.")

def get_collection(collection_name: str):
    return db_instance.db[collection_name]
