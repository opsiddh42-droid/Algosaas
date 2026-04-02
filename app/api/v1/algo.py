from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.api.v1.market import get_kotak_client
from datetime import datetime, timezone, timedelta

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

class AlgoConfig(BaseModel):
    use_default: bool
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float

@router.get("/status")
async def get_algo_status(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    if not state:
        default_config = {"use_default": True, "index": "NIFTY", "entry_time": "10:00", "max_premium": 6.0, "sl_pct": 200.0}
        state = {"user_id": current_user["id"], "is_active": False, "last_executed_date": "", "config": default_config}
        await algo_col.insert_one(state)
        
    config = state.get("config", {"use_default": True, "index": "NIFTY", "entry_time": "10:00", "max_premium": 6.0, "sl_pct": 200.0})
    
    # Determine Plan Display Name
    if config["use_default"]:
        day = datetime.now(IST).weekday()
        if day in [0, 4]: plan = "Default: NIFTY Sell @ ₹6"
        elif day == 1: plan = "Default: SENSEX Sell @ ₹12"
        else: plan = "Default: Idle Today"
    else:
        plan = f"Custom: {config['index']} Sell @ ₹{config['max_premium']}"

    return {
        "status": "success", 
        "is_active": state.get("is_active", False),
        "plan_today": plan,
        "config": config,
        "executed_today": state.get("last_executed_date") == datetime.now(IST).strftime("%Y-%m-%d")
    }

@router.post("/toggle")
async def toggle_algo(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    new_status = not state.get("is_active", False) if state else True
    
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"is_active": new_status}}, upsert=True)
    return {"status": "success", "is_active": new_status, "message": "Algo Bot Turned ON!" if new_status else "Algo Bot Turned OFF."}

@router.post("/config")
async def update_algo_config(config: AlgoConfig, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"config": config.dict()}}, upsert=True)
    return {"status": "success", "message": "Algo Strategy Settings Saved!"}

@router.post("/execute-now")
async def manual_trigger_algo(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    now = datetime.now(IST)
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    config = state.get("config", {"use_default": True}) if state else {"use_default": True}

    # 1. APPLY LOGIC (Default vs Custom)
    if config.get("use_default", True):
        day = now.weekday()
        if day not in [0, 1, 4]: return {"status": "error", "message": "No default strategy planned for today."}
        index = "SENSEX" if day == 1 else "NIFTY"
        target_premium = 12.0 if day == 1 else 6.0
        sl_pct = 200.0
    else:
        index = config.get("index", "NIFTY")
        target_premium = float(config.get("max_premium", 6.0))
        sl_pct = float(config.get("sl_pct", 200.0))

    qty = "10" if index == "SENSEX" else "15" if index == "BANKNIFTY" else "50"
    coll_name = f"{index.lower()}_strike_data"

    # 2. GET LIVE DATA
    client = get_kotak_client(current_user["id"])
    coll = get_collection(coll_name)
    cursor = await coll.find().to_list(length=None)
    if not cursor:
        raise HTTPException(status_code=400, detail=f"No {index} data in DB. Please Update Weekly Expiry first!")

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

    # 3. FIND PREMIUM MATCH
    ce_list.sort(key=lambda x: x["ltp"], reverse=True)
    pe_list.sort(key=lambda x: x["ltp"], reverse=True)

    best_ce = next((x for x in ce_list if x["ltp"] <= target_premium), None)
    best_pe = next((x for x in pe_list if x["ltp"] <= target_premium), None)

    if not best_ce or not best_pe:
        return {"status": "error", "message": f"Could not find {index} CE/PE below ₹{target_premium}"}

    # 4. CALCULATE SL
    ce_sl = round(best_ce["ltp"] * (1 + sl_pct/100), 1)
    pe_sl = round(best_pe["ltp"] * (1 + sl_pct/100), 1)

    # 5. FIRE PAPER ORDERS
    paper_col = get_collection("paper_trades" if mode == "PAPER" else "real_trades")
    trade_docs = [
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True,
         "legs": [{"symbol": best_ce["sym"], "token": best_ce["tk"], "transaction": "S", "qty": int(qty), "entry_price": best_ce["ltp"], "ltp": best_ce["ltp"], "sl": ce_sl, "target": 0, "status": "OPEN"}]},
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True,
         "legs": [{"symbol": best_pe["sym"], "token": best_pe["tk"], "transaction": "S", "qty": int(qty), "entry_price": best_pe["ltp"], "ltp": best_pe["ltp"], "sl": pe_sl, "target": 0, "status": "OPEN"}]}
    ]
    await paper_col.insert_many(trade_docs)
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"last_executed_date": now.strftime("%Y-%m-%d")}})

    return {"status": "success", "message": f"Executed! Sold {best_ce['sym']} @ ₹{best_ce['ltp']} & {best_pe['sym']} @ ₹{best_pe['ltp']}"}
