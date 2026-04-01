from fastapi import APIRouter, Depends, HTTPException
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.api.v1.market import get_kotak_client
from datetime import datetime, timezone, timedelta
import pandas as pd
import time

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

@router.get("/status")
async def get_algo_status(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    if not state:
        state = {"user_id": current_user["id"], "is_active": False, "last_executed_date": "", "trades": []}
        await algo_col.insert_one(state)
        
    # Find what strategy is active today
    day = datetime.now(IST).weekday() # 0=Mon, 1=Tue, 4=Fri
    plan = "Idle (No Plan Today)"
    if day in [0, 4]: plan = "NIFTY CE/PE Sell @ ₹6"
    elif day == 1: plan = "SENSEX CE/PE Sell @ ₹12"

    return {
        "status": "success", 
        "is_active": state.get("is_active", False),
        "plan_today": plan,
        "executed_today": state.get("last_executed_date") == datetime.now(IST).strftime("%Y-%m-%d"),
        "trades": state.get("trades", [])
    }

@router.post("/toggle")
async def toggle_algo(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    new_status = not state.get("is_active", False) if state else True
    
    await algo_col.update_one(
        {"user_id": current_user["id"]}, 
        {"$set": {"is_active": new_status}}, 
        upsert=True
    )
    
    msg = "🟢 Algo Bot Turned ON! It will execute at 10:00 AM." if new_status else "🔴 Algo Bot Turned OFF."
    return {"status": "success", "is_active": new_status, "message": msg}

# 🟢 THE CORE ALGO ENGINE (To be called by background task or manual trigger)
@router.post("/execute-now")
async def manual_trigger_algo(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    now = datetime.now(IST)
    if now.hour < 10:
        return {"status": "error", "message": "It's not 10:00 AM yet."}
        
    day = now.weekday()
    if day not in [0, 1, 4]:
        return {"status": "error", "message": "No strategy configured for today."}

    index = "SENSEX" if day == 1 else "NIFTY"
    target_premium = 12.0 if day == 1 else 6.0
    qty = "10" if index == "SENSEX" else "50"
    coll_name = "sensex_strike_data" if index == "SENSEX" else "nifty_strike_data"

    # 1. Get Live Data
    client = get_kotak_client(current_user["id"])
    coll = get_collection(coll_name)
    cursor = await coll.find().to_list(length=None)
    if not cursor:
        raise HTTPException(status_code=400, detail=f"No {index} data in DB. Sync Master first!")

    tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": "bse_fo" if index=="SENSEX" else "nse_fo"} for doc in cursor]
    
    ce_list, pe_list = [], []
    for i in range(0, len(tokens_req), 50):
        batch = tokens_req[i:i+50]
        try:
            q = client.quotes(instrument_tokens=batch, quote_type="all")
            raw = q if isinstance(q, list) else q.get('data', [])
            for item in raw:
                tk = str(item.get('exchange_token') or item.get('tk'))
                ltp = float(item.get('ltp', 0))
                doc = next((d for d in cursor if d["Token"] == tk), None)
                if doc and ltp > 0:
                    if doc["Type"] == "CE": ce_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp})
                    else: pe_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp})
        except: pass

    # 2. Find Premium <= Target (Closest to Target)
    ce_list.sort(key=lambda x: x["ltp"], reverse=True) # Sort descending
    pe_list.sort(key=lambda x: x["ltp"], reverse=True)

    best_ce = next((x for x in ce_list if x["ltp"] <= target_premium), None)
    best_pe = next((x for x in pe_list if x["ltp"] <= target_premium), None)

    if not best_ce or not best_pe:
        return {"status": "error", "message": "Could not find options matching the premium criteria."}

    # 3. Calculate Stoploss (200% loss means SL is 3x Premium)
    ce_sl = round(best_ce["ltp"] * 3, 1)
    pe_sl = round(best_pe["ltp"] * 3, 1)

    # 4. Fire Orders (PAPER LOGIC FOR NOW)
    paper_col = get_collection("paper_trades")
    trade_docs = [
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True,
         "legs": [{"symbol": best_ce["sym"], "token": best_ce["tk"], "transaction": "S", "qty": int(qty), "entry_price": best_ce["ltp"], "ltp": best_ce["ltp"], "sl": ce_sl, "target": 0, "status": "OPEN"}]},
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True,
         "legs": [{"symbol": best_pe["sym"], "token": best_pe["tk"], "transaction": "S", "qty": int(qty), "entry_price": best_pe["ltp"], "ltp": best_pe["ltp"], "sl": pe_sl, "target": 0, "status": "OPEN"}]}
    ]
    await paper_col.insert_many(trade_docs)

    # Update state
    algo_col = get_collection("algo_state")
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"last_executed_date": now.strftime("%Y-%m-%d")}})

    return {"status": "success", "message": f"Auto-Executed! Sold {best_ce['sym']} @ {best_ce['ltp']} and {best_pe['sym']} @ {best_pe['ltp']}"}
