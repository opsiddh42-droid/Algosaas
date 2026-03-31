from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime
import pytz
import asyncio
from neo_api_client import NeoAPI

from app.core.database import get_collection

# --- HELPER: KOTAK CLIENT ---
def get_kotak_client(db_user):
    try:
        return NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
    except Exception as e:
        print(f"Failed to init Kotak Client for {db_user.get('email')}")
        return None

# ==========================================
# 🚀 CORE ENGINE: CHECK & EXECUTE STRATEGIES
# ==========================================
async def check_and_execute_strategies():
    # Indian Standard Time (IST) set karein
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M") # Format: "09:35"

    # Weekend (Sat/Sun) ya Market close hone par engine ko sula do
    if now.weekday() >= 5 or current_time < "09:15" or current_time > "15:30":
        return

    try:
        strat_col = get_collection("strategies")
        users_col = get_collection("users")

        # Database se saari ACTIVE strategies nikalo
        active_strats = await strat_col.find({"is_active": True}).to_list(length=1000)

        for strat in active_strats:
            user_id = strat.get("user_id")
            
            # 🟢 ENTRY LOGIC (Agar Entry Time match ho gaya)
            if strat.get("entryTime") == current_time:
                print(f"⚡ [ENTRY TRIGGERED] Strategy: {strat['strategy_id']} for User: {user_id}")
                
                # User ka Kotak details nikalo
                db_user = await users_col.find_one({"id": user_id})
                if not db_user or db_user.get("kotak_status") != "Active":
                    continue
                
                client = get_kotak_client(db_user)
                if not client: continue

                # Saare Legs (CE/PE, Buy/Sell) execute karo
                for leg in strat.get("legs", []):
                    qty = str(int(leg["lot"]) * 25) # Note: Nifty ka lot size 25 hai. BankNifty ke liye dynamic karna padega.
                    transaction = "B" if leg["position"] == "Buy" else "S"
                    
                    # Note: Asli live trading mein yahan 'trading_symbol' ko ATM calculate karke banana padta hai.
                    # Jaise: 'NIFTY24APR22000CE'. Abhi engine ko signal bhej rahe hain:
                    print(f"➡️ Firing Order: {transaction} {qty} Qty | {strat['index']} {leg['optType']} ({leg['strikeType']})")
                    
                    # Live Market Code (Jab ATM calculate ho jaye):
                    # client.place_order(exchange_segment="nse_fo", product="NRML", price="0", order_type="MKT", quantity=qty, validity="DAY", trading_symbol=DYNAMIC_SYMBOL, transaction_type=transaction, amo="NO")

                # Entry hone ke baad, hum isko mark kar sakte hain ki aaj ka trade ho gaya taaki double entry na ho

            # 🔴 EXIT LOGIC (Agar Exit Time match ho gaya)
            elif strat.get("exitTime") == current_time:
                print(f"🚨 [EXIT TRIGGERED] Strategy: {strat['strategy_id']} for User: {user_id}")
                # Yahan par aap '/exit-all' wala logic call kar sakte hain ya legs ko reverse (Buy ko Sell) kar sakte hain.


    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# ==========================================
# ⏰ SCHEDULER START FUNCTION
# ==========================================
def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Kolkata'))
    
    # Har minute ke exactly 0th second par ye function chalega (e.g. 09:35:00)
    scheduler.add_job(check_and_execute_strategies, 'cron', second='0')
    
    scheduler.start()
    print("🤖 AlgoSaaS Pro - Auto-Trader Engine Started!")
