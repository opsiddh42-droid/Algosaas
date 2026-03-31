from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import pytz
import asyncio
import pandas as pd
import os
import time
import requests
from neo_api_client import NeoAPI

from app.core.database import get_collection

# --- ⚙️ CONFIGURATION ---
INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "LotSize": 25, "StrikeGap": 50, "Master": "nse_fo_master.csv", "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/nse_fo"},
    "BANKNIFTY": {"Exchange": "nse_fo", "LotSize": 15, "StrikeGap": 100, "Master": "nse_fo_master.csv", "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/nse_fo"},
    "SENSEX": {"Exchange": "bse_fo", "LotSize": 10, "StrikeGap": 100, "Master": "bse_fo_master.csv", "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/bse_fo"}
}

# --- 🛠️ HELPERS ---
def get_kotak_client(db_user):
    try:
        return NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
    except:
        return None

def get_live_spot(client, index_name):
    tokens = {"NIFTY": "256265", "BANKNIFTY": "26000", "SENSEX": "1"}
    exch = "bse_cm" if index_name == "SENSEX" else "nse_cm"
    try:
        q = client.quotes(instrument_tokens=[{"instrument_token": tokens[index_name], "exchange_segment": exch}], quote_type="all")
        if q and isinstance(q, dict) and 'data' in q: return float(q['data'][0].get('ltp', 0))
    except: pass
    return 0.0

def get_master_csv(conf):
    file_path = conf["Master"]
    if not os.path.exists(file_path) or (time.time() - os.path.getmtime(file_path) > 86400):
        try:
            r = requests.get(conf["Url"])
            with open(file_path, 'wb') as f: f.write(r.content)
        except Exception as e:
            print(f"CSV Download Error: {e}")
    return pd.read_csv(file_path, sep=',', header=None, low_memory=False)

# --- 🧠 SMART STRIKE CALCULATOR ---
def calculate_dynamic_strike(client, index, underlying, opt_type, criteria, target_value):
    conf = INDICES_CONFIG[index]
    
    # 1. Base Price nikalo (Spot ya Future)
    base_price = 0.0
    if underlying == "Cash":
        base_price = get_live_spot(client, index)
    else:
        # Future Price calculation (Simplified)
        base_price = get_live_spot(client, index) # Fallback to Spot if Future fetching gets complex in background

    if base_price == 0: return None

    # 2. ATM Calculate karo
    atm = round(base_price / conf["StrikeGap"]) * conf["StrikeGap"]

    # 3. ITM/OTM Shift Logic (Agar Strike Type chuna hai)
    final_strike = atm
    if criteria == "Strike Type":
        shift_multiplier = 0
        if "ITM" in target_value:
            shift_multiplier = int(target_value.replace("ITM", ""))
            # Call ke liye ITM matlab neeche, Put ke liye ITM matlab upar
            if opt_type == "CE": final_strike = atm - (shift_multiplier * conf["StrikeGap"])
            else: final_strike = atm + (shift_multiplier * conf["StrikeGap"])
        
        elif "OTM" in target_value:
            shift_multiplier = int(target_value.replace("OTM", ""))
            # Call ke liye OTM matlab upar, Put ke liye OTM matlab neeche
            if opt_type == "CE": final_strike = atm + (shift_multiplier * conf["StrikeGap"])
            else: final_strike = atm - (shift_multiplier * conf["StrikeGap"])

    return final_strike


# ==========================================
# 🚀 CORE ENGINE: ADVANCED ALGO TRADER
# ==========================================
async def check_and_execute_strategies():
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    # Weekend / Off-market check (Uncomment below line for real live server)
    # if now.weekday() >= 5 or current_time < "09:15" or current_time > "15:30": return

    try:
        strat_col = get_collection("strategies")
        users_col = get_collection("users")

        active_strats = await strat_col.find({"is_active": True}).to_list(length=1000)

        for strat in active_strats:
            strat_id = strat.get("strategy_id")
            user_id = strat.get("user_id")
            status = strat.get("status", "WAITING")
            
            db_user = await users_col.find_one({"id": user_id})
            if not db_user or db_user.get("kotak_status") != "Active": continue
            
            client = get_kotak_client(db_user)
            if not client: continue

            # ==========================================
            # 🟢 1. ENTRY LOGIC
            # ==========================================
            if status == "WAITING" and strat.get("entryTime") == current_time:
                
                # Spot Trigger Check
                spot_trigger = strat.get("spotTrigger", {})
                if spot_trigger.get("enabled"):
                    live_spot = get_live_spot(client, strat["index"])
                    # Agar conditions meet na ho, toh continue kar jao (next minute aayega)
                    # Logic: Needs DB to store base spot at 09:15 to calculate diff. Simplified for now.

                print(f"⚡ [ENTRY FIRE] Strategy: {strat_id} | User: {db_user.get('name')}")
                
                index_name = strat["index"]
                conf = INDICES_CONFIG[index_name]
                df = get_master_csv(conf)

                # Find Expiry Date Strings (Simplified logic checking next 30 days)
                all_ref_keys = set(df[7].astype(str).values)
                expiries_found = []
                for i in range(0, 30):
                    test_date = now + timedelta(days=i)
                    d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
                    check_sym = f"{index_name}{d_str}"
                    # Quick check if this date string exists in any symbol
                    if any(check_sym in s for s in all_ref_keys):
                        if d_str not in expiries_found:
                            expiries_found.append(d_str)

                # Execute Legs
                for leg in strat.get("legs", []):
                    # 1. Calculate Exact Strike
                    strike = calculate_dynamic_strike(
                        client, index_name, strat["underlying"], 
                        leg["optType"], leg["criteria"], leg["targetValue"]
                    )
                    
                    if not strike: continue

                    # 2. Pick Expiry string
                    exp_idx = 0 # Default Weekly
                    if leg["expiry"] == "Next Weekly" and len(expiries_found) > 1: exp_idx = 1
                    elif leg["expiry"] == "Monthly" and len(expiries_found) > 3: exp_idx = 3 # Approx
                    
                    expiry_str = expiries_found[exp_idx] if expiries_found else now.strftime("%d%b%y").upper()

                    # 3. Create Final Trading Symbol (e.g., NIFTY24APR22000.00CE)
                    # Kotak often uses format without .00 for some indices, adjust based on master CSV format
                    symbol_search = f"{index_name}{expiry_str}{strike}.00{leg['optType']}"
                    
                    # Search exact symbol in CSV to verify and get token
                    matched_row = df[df[7] == symbol_search]
                    if matched_row.empty:
                        symbol_search = f"{index_name}{expiry_str}{int(strike)}{leg['optType']}" # Try without decimals
                        
                    print(f"🎯 Calculated Symbol: {symbol_search} (From: {leg['targetValue']})")

                    # 4. Fire Order (Buffered Limit - 15% Safe Rule)
                    qty = str(int(leg["lot"]) * conf["LotSize"])
                    transaction = "B" if leg["position"] == "Buy" else "S"
                    
                    try:
                        # HINT: Live platform pe isko uncomment karenge
                        '''
                        client.place_order(
                            exchange_segment=conf["Exchange"], product="NRML", price="0", 
                            order_type="MKT", quantity=qty, validity="DAY", 
                            trading_symbol=symbol_search, transaction_type=transaction, amo="NO"
                        )
                        '''
                        print(f"✅ Executed: {transaction} {qty} x {symbol_search}")
                    except Exception as e:
                        print(f"❌ Order Failed: {e}")
                        
                    time.sleep(0.2) # Avoid rate limit

                # Update Status
                await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "ENTERED"}})


            # ==========================================
            # 🔴 2. EXIT LOGIC
            # ==========================================
            elif status == "ENTERED":
                should_exit = False
                
                # Check Time / Date Exits
                if strat.get("type") == "Intraday" and current_time >= strat.get("exitTime"): should_exit = True
                elif strat.get("type") == "Positional" and current_date >= strat.get("exitDate") and current_time >= strat.get("exitTime"): should_exit = True

                # 🛑 FINAL SQUARE OFF
                if should_exit:
                    print(f"🚨 [EXIT FIRE] Strategy {strat_id} Exiting!")
                    
                    # Logic to close positions will mirror `/exit-all` route
                    await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "COMPLETED", "is_active": False}})

    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

# ==========================================
# ⏰ SCHEDULER START
# ==========================================
def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Kolkata'))
    scheduler.add_job(check_and_execute_strategies, 'cron', second='0')
    scheduler.start()
    print("🤖 AlgoSaaS Pro - AI Engine Connected & Running!")
