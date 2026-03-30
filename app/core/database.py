from motor.motor_asyncio import AsyncIOMotorClient
from app.core.config import settings
import logging

# Logger setup taaki errors easily track ho sakein
logger = logging.getLogger(__name__)

class Database:
    client: AsyncIOMotorClient = None
    db = None

db_instance = Database()

async def connect_to_mongo():
    try:
        logger.info("⏳ Connecting to MongoDB...")
        db_instance.client = AsyncIOMotorClient(settings.MONGO_URI)
        db_instance.db = db_instance.client[settings.DATABASE_NAME]
        logger.info("✅ MongoDB Connected Successfully!")
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Failed: {e}")

async def close_mongo_connection():
    if db_instance.client:
        db_instance.client.close()
        logger.info("🛑 MongoDB Connection Closed.")

# Helper function kisi bhi collection ko easily access karne ke liye
def get_collection(collection_name: str):
    return db_instance.db[collection_name]
