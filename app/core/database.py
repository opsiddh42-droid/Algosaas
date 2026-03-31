async def connect_to_mongo():
    try:
        logger.info("⏳ Connecting to MongoDB...")
        db_instance.client = AsyncIOMotorClient(settings.MONGO_URI)
        
        # 🟢 YAHAN FIX HAI: settings hata kar seedha naam likh diya!
        db_instance.db = db_instance.client["tradingbot"] 
        
        logger.info("✅ MongoDB Connected Successfully!")
    except Exception as e:
        logger.error(f"❌ MongoDB Connection Failed: {e}")
