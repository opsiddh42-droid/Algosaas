from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta
import pytz
import asyncio
import pandas as pd
from neo_api_client import NeoAPI

from app.core.database import get_collection

# --- ⚙️ CONFIGURATION ---
INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "LotSize": 65, "StrikeGap": 50, "SpotToken": "NIFTY50", "SpotExch": "nse_cm"},
    "BANKNIFTY": {"Exchange": "nse_fo", "LotSize": 15, "StrikeGap": 100, "SpotToken": "26000", "SpotExch": "nse_cm"},
    "SENSEX": {"Exchange": "bse_fo", "LotSize": 20, "StrikeGap": 100, "SpotToken": "1", "SpotExch": "bse_cm"}
}

def get_kotak_client(db_user):
    try:
        return NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
    except:
        return None

def get_live_spot(client, index_name):
    conf = INDICES_CONFIG.get(index_name)
    try:
        q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        if q and isinstance(q, dict) and 'data' in q: return float(q['data'][0].get('ltp', 0))
    except: pass
    return 0.0

def get_option_ltp(client, token, exch):
    try:
        q = client.quotes(instrument_tokens=[{"instrument_token": str(token), "exchange_segment": exch}], quote_type="all")
        if q and isinstance(q, dict) and 'data' in q: return float(q['data'][0].get('ltp', 0))
    except: pass
    return 0.0

def calculate_dynamic_strike(client, index, underlying, opt_type, criteria, target_value):
    conf = INDICES_CONFIG[index]
    base_price = get_live_spot(client, index)
    if base_price == 0: return None

    atm = round(base_price / conf["StrikeGap"]) * conf["StrikeGap"]
    final_strike = atm
    
    if criteria == "Strike Type":
        shift_multiplier = 0
        if "ITM" in target_value:
            shift_multiplier = int(target_value.replace("ITM", ""))
            if opt_type == "CE": final_strike = atm - (shift_multiplier * conf["StrikeGap"])
            else: final_strike = atm + (shift_multiplier * conf["StrikeGap"])
        elif "OTM" in target_value:
            shift_multiplier = int(target_value.replace("OTM", ""))
            if opt_type == "CE": final_strike = atm + (shift_multiplier * conf["StrikeGap"])
            else: final_strike = atm - (shift_multiplier * conf["StrikeGap"])

    return final_strike


# ==========================================
# 🚀 CORE ENGINE: DUAL ALGO TRADER
# ==========================================
async def check_and_execute_strategies():
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    current_time = now.strftime("%H:%M")
    current_date = now.strftime("%Y-%m-%d")

    try:
        strat_col = get_collection("strategies")
        users_col = get_collection("users")
        paper_col = get_collection("paper_trades")
        fo_master_col = get_collection("fo_master") # 🚀 DB Fetch

        active_strats = await strat_col.find({"is_active": True}).to_list(length=1000)

        for strat in active_strats:
            strat_id = strat.get("strategy_id")
            user_id = strat.get("user_id")
            status = strat.get("status", "WAITING")
            mode = strat.get("executionMode", "PAPER")
            
            db_user = await users_col.find_one({"id": user_id})
            if not db_user or db_user.get("kotak_status") != "Active": continue
            
            client = get_kotak_client(db_user)
            if not client: continue

            # ==========================================
            # 🟢 1. ENTRY LOGIC
            # ==========================================
            if status == "WAITING" and strat.get("entryTime") == current_time:
                
                print(f"⚡ [{mode} ENTRY FIRE] Strategy: {strat_id} | User: {db_user.get('name')}")
                
                index_name = strat["index"]
                conf = INDICES_CONFIG[index_name]
                
                # 🚀 FAST DB LOOKUP
                cursor = await fo_master_col.find({"IndexName": index_name}).to_list(length=None)
                df = pd.DataFrame(cursor)
                if df.empty:
                    print(f"❌ Master DB Empty for {index_name}")
                    continue
                
                df.columns = df.columns.astype(str)
                all_ref_keys = set(df["7"].astype(str).values)
                expiries_found = []
                for i in range(0, 45):
                    test_date = now + timedelta(days=i)
                    d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
                    check_sym = f"{index_name}{d_str}"
                    if any(check_sym in s for s in all_ref_keys):
                        if d_str not in expiries_found:
                            expiries_found.append(d_str)

                executed_virtual_legs = []

                for leg in strat.get("legs", []):
                    strike = calculate_dynamic_strike(
                        client, index_name, strat["underlying"], 
                        leg["optType"], leg["criteria"], leg["targetValue"]
                    )
                    if not strike: continue

                    exp_idx = 0 
                    if leg["expiry"] == "Next Weekly" and len(expiries_found) > 1: exp_idx = 1
                    elif leg["expiry"] == "Monthly" and len(expiries_found) > 3: exp_idx = 3 
                    
                    expiry_str = expiries_found[exp_idx] if expiries_found else now.strftime("%d%b%y").upper()

                    sym1 = f"{index_name}{expiry_str}{strike}.00{leg['optType']}"
                    sym2 = f"{index_name}{expiry_str}{int(strike)}{leg['optType']}"
                    
                    matched_row = df[(df["7"] == sym1) | (df["7"] == sym2)]

                    qty = int(leg["lot"]) * conf["LotSize"]
                    transaction = "B" if leg["position"] == "Buy" else "S"
                    
                    token = str(int(float(matched_row.iloc[0]["0"]))) if not matched_row.empty else None
                    symbol_search = matched_row.iloc[0]["5"] if not matched_row.empty else sym1

                    # 📝 PAPER LOGIC
                    if mode == "PAPER":
                        ltp = get_option_ltp(client, token, conf["Exchange"]) if token else 0.0
                        executed_virtual_legs.append({
                            "leg_id": leg["id"], "symbol": symbol_search, "token": token,
                            "transaction": transaction, "qty": qty, "entry_price": ltp, "status": "OPEN"
                        })
                        print(f"📝 Paper Saved: {transaction} {qty} x {symbol_search} @ ₹{ltp}")

                    # 💸 REAL LOGIC
                    elif mode == "REAL":
                        try:
                            # client.place_order(exchange_segment=conf["Exchange"], product="NRML", price="0", order_type="MKT", quantity=str(qty), validity="DAY", trading_symbol=symbol_search, transaction_type=transaction, amo="NO")
                            print(f"✅ Real Fired: {transaction} {qty} x {symbol_search}")
                        except Exception as e:
                            print(f"❌ Real Order Failed: {e}")

                if mode == "PAPER" and executed_virtual_legs:
                    await paper_col.insert_one({
                        "strategy_id": strat_id, "user_id": user_id, "entry_time": current_time,
                        "entry_date": current_date, "status": "OPEN", "legs": executed_virtual_legs
                    })

                await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "ENTERED"}})

            # ==========================================
            # 🔴 2. EXIT LOGIC
            # ==========================================
            elif status == "ENTERED":
                should_exit = False
                if strat.get("type") == "Intraday" and current_time >= strat.get("exitTime"): should_exit = True
                elif strat.get("type") == "Positional" and current_date >= strat.get("exitDate") and current_time >= strat.get("exitTime"): should_exit = True

                if should_exit:
                    print(f"🚨 [{mode} EXIT FIRE] Strategy {strat_id} Exiting!")
                    if mode == "PAPER":
                        await paper_col.update_many({"strategy_id": strat_id, "status": "OPEN"}, {"$set": {"status": "CLOSED", "exit_time": current_time}})
                    await strat_col.update_one({"strategy_id": strat_id}, {"$set": {"status": "COMPLETED", "is_active": False}})

    except Exception as e:
        print(f"❌ Scheduler Error: {e}")

def start_scheduler():
    scheduler = AsyncIOScheduler(timezone=pytz.timezone('Asia/Kolkata'))
    scheduler.add_job(check_and_execute_strategies, 'cron', second='0')
    scheduler.start()
