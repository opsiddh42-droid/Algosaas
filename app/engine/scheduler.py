from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import pytz
import logging
from app.core.database import get_collection
from app.engine.risk_manager import execute_safe_exit_all

logger = logging.getLogger(__name__)

# Indian Standard Time (IST) set karna zaroori hai
ist_tz = pytz.timezone('Asia/Kolkata')

async def check_and_execute_strategies():
    """Yeh function har minute chalega aur active strategies check karega"""
    now = datetime.now(ist_tz)
    current_time_str = now.strftime("%H:%M")
    
    logger.info(f"⏱️ Scheduler Ping: Checking strategies for time {current_time_str}")
    
    strat_col = get_collection("strategies")
    
    # Sirf un strategies ko fetch karo jo ON (is_active: True) hain
    cursor = strat_col.find({"is_active": True})
    active_strategies = await cursor.to_list(length=5000)
    
    for strat in active_strategies:
        user_id = strat.get("user_id")
        strat_name = strat.get("strategy_name")
        
        # 1. ENTRY LOGIC TRIGGER
        if strat.get("entry_time") == current_time_str:
            logger.info(f"🚀 TRIGGER ENTRY: {strat_name} for User {user_id}")
            # Yahan hum aage chalkar Kotak API ka order fire function call karenge
            # execute_strategy_entry(strat, user_id)
            
        # 2. EXIT LOGIC TRIGGER
        elif strat.get("exit_time") == current_time_str:
            logger.info(f"🛑 TRIGGER EXIT: {strat_name} for User {user_id}")
            # Agar exit time ho gaya, toh hum apna Safe Exit algo call kar denge
            # Note: Iske liye user ka Kotak session zaroori hoga jo redis/DB se aayega
            # await execute_safe_exit_all(user_id, kotak_client)

def start_scheduler():
    """Scheduler ko initialize aur start karne ka function"""
    scheduler = AsyncIOScheduler(timezone=ist_tz)
    
    # Har 1 minute baad check_and_execute_strategies function ko run karna
    scheduler.add_job(check_and_execute_strategies, 'cron', minute='*')
    
    scheduler.start()
    logger.info("⚙️ Execution Scheduler Started! Monitoring live market time...")
