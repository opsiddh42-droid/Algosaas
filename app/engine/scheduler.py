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

# --- HELPER: GET LIVE SPOT PRICE ---
def get_live_spot(client, index_name):
    # Yeh Kotak se Nifty/BankNifty ka live price layega (Spot Momentum check karne ke liye)
    tokens = {
        "NIFTY": "256265",      # Nifty 50 Index Token (NSE)
        "BANKNIFTY": "26000",   # BankNifty Index Token
        "SENSEX": "1"           # Sensex Token (BSE)
    }
    exch = "bse_cm" if index_name == "SENSEX" else "nse_cm"
    token = tokens.get(index_name, "256265")
    
    try:
        q = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": exch}], quote_type="all")
        if q and isinstance(q, dict) and 'data' in q:
            return float(q['data'][0].get('ltp', 0))
        elif q and isinstance(q, list):
            return float(q[0].get('ltp', 0))
    except:
        return 0.0
    return 0.0

# ==========================================
# 🚀 CORE ENGINE: ADVANCED ALGO TRADER
# ==========================================
async def check_and_execute_strategies():
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M") # "09:35"
    current_date = now.strftime("%Y-%m-%d") # "2024-04-12"

    # Weekend ya market close time par engine rest karega (Except agar BTST/Positional ki processing karni ho)
    if current_time < "09:15" or current_time > "15:30":
        return

    try:
        strat_col = get_collection("strategies")
        users_col = get_collection("users")

        # Database se saari ACTIVE strategies nikalo
        active_strats = await strat_col.find({"is_active": True}).to_list(length=1000)

        for strat in active_strats:
            strat_id = strat.get("strategy_id")
            user_id = strat.get("user_id")
            status = strat.get("status", "WAITING") # Status: WAITING, ENTERED, COMPLETED
            
            # User Data & Kotak Client
            db_user = await users_col.find_one({"id": user_id})
            if not db_user or db_user.get("kotak_status") != "Active":
                continue
            
            client = get_kotak_client(db_user)
            if not client: continue

            # ==========================================
            # 🟢 1. ENTRY LOGIC (Agar trade abhi tak nahi li hai)
            # ==========================================
            if status == "WAITING" and strat.get("entryTime") == current_time:
                
                # Check Spot Momentum Trigger (Agar ON hai)
                spot_trigger = strat.get("spotTrigger", {})
                if spot_trigger.get("enabled"):
                    live_spot = get_live_spot(client, strat["index"])
                    # Yahan logic aayega ki "kya spot price trigger point cross kar chuka hai?"
                    # For now, print kar rahe hain
                    print(f"📊 Spot Check: {strat['index']} is at {live_spot}. Need to check if it moved {spot_trigger.get('points')} points {spot_trigger.get('direction')}.")
                    # Agar condition match nahi hui, toh 'continue' karke agle minute wait karenge
                    
                print(f"⚡ [ENTRY FIRE] Strategy: {strat_id} | User: {user_id}")
                
                # TODO: Option Chain Master CSV se Strike nikal kar Order Fire karna
                # (Yeh code hum already market.py me likh chuke hain, usko yahan integrate karenge)
                
                # Entry ke baad database me status update kar do taaki dobara order na lagaye
                await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "ENTERED"}})


            # ==========================================
            # 🔴 2. EXIT LOGIC (Time-Based & Positional)
            # ==========================================
            elif status == "ENTERED":
                should_exit = False
                exit_reason = ""

                # Check A: Intraday Time Exit
                if strat.get("type") == "Intraday" and current_time >= strat.get("exitTime"):
                    should_exit = True
                    exit_reason = "Intraday Time Reached"

                # Check B: Positional Date & Time Exit
                elif strat.get("type") == "Positional":
                    if current_date >= strat.get("exitDate") and current_time >= strat.get("exitTime"):
                        should_exit = True
                        exit_reason = "Positional Expiry Reached"

                # Check C: Overall Stop Loss (MTM Check)
                overall_sl = strat.get("overallSL", {})
                if not should_exit and overall_sl.get("enabled"):
                    # Kotak se live MTM fetch karo
                    pos_resp = client.positions()
                    total_mtm = 0.0
                    if isinstance(pos_resp, dict) and 'data' in pos_resp:
                        for p in pos_resp['data']:
                            total_mtm += float(p.get('mtm', 0))
                            
                    max_loss = float(overall_sl.get("value", 0))
                    
                    # Agar Total MTM negative me max_loss se zyada gir gaya
                    if total_mtm <= -max_loss:
                        should_exit = True
                        exit_reason = f"Overall SL Hit! (Loss: {total_mtm})"

                # 🛑 FINAL SQUARE OFF EXECUTION
                if should_exit:
                    print(f"🚨 [EXIT FIRE] Strategy {strat_id} exiting. Reason: {exit_reason}")
                    
                    # Yahan Kotak API se positions close karne ka order bheja jayega
                    # Same logic jo humne trades.py ke 'exit-all' me likha hai (Smart Buffer Limit)
                    
                    # Exit ke baad strategy ko COMPLETED mark kar do
                    await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "COMPLETED", "is_active": False}})

    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# ==========================================
# ⏰ SCHEDULER START FUNCTION
# ==========================================
def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Kolkata'))
    # Har minute ke exactly 0th second par Engine check karega
    scheduler.add_job(check_and_execute_strategies, 'cron', second='0')
    scheduler.start()
    print("🤖 AlgoSaaS Pro - Advanced Auto-Trader Engine Started!")
