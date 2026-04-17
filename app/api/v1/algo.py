import os
import asyncio
import httpx
import time
import uuid
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel, Field
from typing import List, Optional
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

# 🟢 TELEGRAM CONFIG 🟢
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50, "SpotToken": "Nifty 50", "SpotExch": "nse_cm", "Coll": "nifty_strike_data"},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100, "SpotToken": "SENSEX", "SpotExch": "nse_cm", "Coll": "sensex_strike_data"},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100, "SpotToken": "Nifty Bank", "SpotExch": "nse_cm", "Coll": "banknifty_strike_data"}
}

# 🟢 IN-MEMORY SNAPSHOTS FOR 1-HOUR CHANGE 🟢
GLOBAL_SNAPSHOTS = {"NIFTY": [], "SENSEX": [], "BANKNIFTY": []}

def record_snapshot(index: str, ce_tot: float, pe_tot: float):
    now = datetime.now(IST)
    GLOBAL_SNAPSHOTS[index].append({"time": now, "ce_tot": ce_tot, "pe_tot": pe_tot})
    cutoff = now - timedelta(hours=2)
    GLOBAL_SNAPSHOTS[index] = [s for s in GLOBAL_SNAPSHOTS[index] if s["time"] >= cutoff]

def get_1h_change(index: str, current_ce_tot: float, current_pe_tot: float):
    if not GLOBAL_SNAPSHOTS[index]: return 0, 0, 0
    target_time = datetime.now(IST) - timedelta(hours=1)
    closest = min(GLOBAL_SNAPSHOTS[index], key=lambda x: abs((x["time"] - target_time).total_seconds()))
    ce_diff = current_ce_tot - closest["ce_tot"]
    pe_diff = current_pe_tot - closest["pe_tot"]
    mins_passed = int((datetime.now(IST) - closest["time"]).total_seconds() / 60)
    return ce_diff, pe_diff, mins_passed

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise HTTPException(status_code=401, detail="Kotak Session OFF! Please Login.")
    return KOTAK_SESSIONS[user_id]

def format_oi(value):
    is_negative = value < 0
    abs_val = abs(value)
    res = ""
    if abs_val >= 10000000: res = f"{abs_val / 10000000:.2f} Cr"
    elif abs_val >= 100000: res = f"{abs_val / 100000:.2f} L"
    else: res = str(int(abs_val))
    return f"-{res}" if is_negative else res

def calculate_max_pain(options_data):
    if not options_data: return 0
    min_loss = float('inf')
    max_pain_strike = 0
    for candidate in options_data:
        current_strike = candidate["strike"]
        total_loss = 0
        for opt in options_data:
            stk = opt["strike"]
            if current_strike > stk: total_loss += (current_strike - stk) * opt.get("ce_oi", 0)
            elif current_strike < stk: total_loss += (stk - current_strike) * opt.get("pe_oi", 0)
        if total_loss < min_loss:
            min_loss = total_loss
            max_pain_strike = current_strike
    return max_pain_strike

def round_to_tick(price: float) -> float:
    return round(price * 20) / 20.0

async def send_user_alert(user_id: str, message: str):
    if not BOT_TOKEN or not TELEGRAM_API_URL: return
    try:
        user_col = get_collection("users")
        user = await user_col.find_one({"id": user_id})
        if user and user.get("telegram_chat_id"):
            chat_id = user["telegram_chat_id"]
            async with httpx.AsyncClient() as client:
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
    except Exception as e: pass

# --- REQUEST MODELS ---
class CustomStrategyConfig(BaseModel):
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float
    active_days: List[int]
    lots: Optional[int] = 1 

class TotalAlertConfig(BaseModel):
    is_active: bool
    index: str

class CustomStrikeConfig(BaseModel):
    is_active: bool
    index: str
    strikes: List[int] = Field(..., max_items=5)
    frequency_minutes: int 

# --- WEBHOOK & ALGO ROUTES ---
@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "message" not in data: return {"status": "ok"}
        chat_id = data["message"]["chat"]["id"]
        text = data["message"].get("text", "").strip().upper()
        
        if text.startswith("/START"):
            msg = "Welcome to AlgoSaaS! 🚀\nPlease send your Kotak Neo UCC code (e.g., KOTAK123) to link your account."
            async with httpx.AsyncClient() as client:
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": msg})
            return {"status": "ok"}
            
        if len(text) >= 3: 
            user_col = get_collection("users") 
            user = await user_col.find_one({"kotak_ucc": text}) 
            async with httpx.AsyncClient() as client:
                if user:
                    await user_col.update_one({"_id": user["_id"]}, {"$set": {"telegram_chat_id": chat_id}})
                    msg = f"✅ Success! Your UCC '{text}' is linked. You will now receive Live Algo Alerts here."
                else:
                    msg = f"❌ UCC '{text}' not found in database. Please configure Kotak Neo in the Web Terminal first."
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": msg})
    except Exception as e:
        pass
    return {"status": "ok"}

@router.get("/status")
async def get_algo_status(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    if not state:
        state = {"user_id": current_user["id"], "is_active": False, "default_active": True, "last_executed_date": "", "custom_strategies": []}
        await algo_col.insert_one(state)
        
    day = datetime.now(IST).weekday()
    if day in [0, 1, 4]: plan = "Default: NIFTY Sell <= ₹6 | SL 200%"
    elif day in [2, 3]: plan = "Default: SENSEX Sell <= ₹12 | SL 300%"
    else: plan = "Default: Idle Today"

    now = datetime.now(IST)
    current_date_str = now.strftime("%Y-%m-%d")
    current_time = now.time()

    custom_strategies = state.get("custom_strategies", [])
    for strat in custom_strategies:
        try:
            strat_time = datetime.strptime(strat.get("entry_time", "10:00"), "%H:%M").time()
            if strat.get("last_executed_date") == current_date_str:
                strat["time_remaining"] = "Executed Today ✅"
            elif day not in strat.get("active_days", []):
                strat["time_remaining"] = "Not Active Today ⏸️"
            elif current_time < strat_time:
                rem = datetime.combine(now.date(), strat_time) - datetime.combine(now.date(), current_time)
                mins = int(rem.total_seconds() // 60)
                strat["time_remaining"] = f"In {mins} mins ⏳"
            else:
                strat["time_remaining"] = "Time Passed ⌛"
        except:
            strat["time_remaining"] = "--"

    return {
        "status": "success", 
        "is_active": state.get("is_active", False),
        "default_active": state.get("default_active", True),
        "plan_today": plan, 
        "custom_strategies": custom_strategies,
        "executed_today": state.get("last_executed_date") == current_date_str
    }

@router.post("/toggle-master")
async def toggle_master_algo(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    new_status = not state.get("is_active", False) if state else True
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"is_active": new_status}}, upsert=True)
    return {"status": "success", "is_active": new_status, "message": "Master Bot Switch Updated!"}

@router.post("/toggle-default")
async def toggle_default_algo(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    new_status = not state.get("default_active", True) if state else False
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"default_active": new_status}}, upsert=True)
    return {"status": "success", "default_active": new_status, "message": "Default Strategy Updated!"}

@router.post("/toggle")
async def old_toggle_compatibility(current_user: dict = Depends(get_current_user)):
    return await toggle_master_algo(current_user)

@router.post("/add-strategy")
async def add_custom_strategy(strat: CustomStrategyConfig, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    custom_strats = state.get("custom_strategies", []) if state else []
    if len(custom_strats) >= 3:
        return {"status": "error", "message": "Max 3 custom strategies allowed. Please delete one first."}
    
    new_strat = strat.dict()
    new_strat["id"] = str(uuid.uuid4())[:8]
    new_strat["is_active"] = True
    new_strat["last_executed_date"] = ""
    
    if not new_strat.get("lots") or new_strat.get("lots") <= 0:
        new_strat["lots"] = 1
    
    await algo_col.update_one({"user_id": current_user["id"]}, {"$push": {"custom_strategies": new_strat}}, upsert=True)
    return {"status": "success", "message": "New Strategy Added Successfully!"}

@router.delete("/delete-strategy/{strat_id}")
async def delete_custom_strategy(strat_id: str, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    await algo_col.update_one({"user_id": current_user["id"]}, {"$pull": {"custom_strategies": {"id": strat_id}}})
    return {"status": "success", "message": "Strategy Deleted."}

@router.post("/toggle-strategy/{strat_id}")
async def toggle_custom_strategy(strat_id: str, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    if not state: return {"status": "error", "message": "State not found."}
    
    for i, s in enumerate(state.get("custom_strategies", [])):
        if s["id"] == strat_id:
            new_val = not s.get("is_active", True)
            await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {f"custom_strategies.{i}.is_active": new_val}})
            return {"status": "success", "is_active": new_val, "message": "Strategy Status Updated."}
            
    return {"status": "error", "message": "Strategy ID not found."}

async def core_algo_execution(user_id: str, mode: str, strategy: dict = None):
    now = datetime.now(IST)
    algo_col = get_collection("algo_state")
    day = now.weekday()

    if strategy is None:
        if day in [0, 1, 4]: 
            index, target_premium, sl_pct = "NIFTY", 6.0, 200.0
        elif day in [2, 3]: 
            index, target_premium, sl_pct = "SENSEX", 12.0, 300.0
        else:
            return {"status": "error", "message": "No default strategy planned for today."}
        lots = 1
    else:
        if day not in strategy.get("active_days", []): 
            return {"status": "error", "message": "Custom strategy is not configured to run today."}
        index = strategy.get("index", "NIFTY")
        target_premium = float(strategy.get("max_premium", 10.0))
        sl_pct = float(strategy.get("sl_pct", 200.0))
        lots = int(strategy.get("lots") or 1)
        if lots < 1: lots = 1

    if index == "NIFTY": lot_size = 65
    elif index == "BANKNIFTY": lot_size = 30
    elif index == "SENSEX": lot_size = 20
    else: lot_size = 65

    qty = str(lots * lot_size)

    try: client = get_kotak_client(user_id)
    except HTTPException: return {"status": "error", "message": "Kotak Session OFF! Please Login."}

    conf = INDICES_CONFIG.get(index)
    try:
        q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
        spot_ltp = float(item.get('ltp', 0))
        if spot_ltp == 0 and index == "SENSEX":
            q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
            item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
            spot_ltp = float(item.get('ltp', 0))
    except:
        spot_ltp = 0
        
    coll_name, exch_seg = conf["Coll"], conf["Exchange"]
    coll = get_collection(coll_name)
    cursor = await coll.find().to_list(length=None)
    if not cursor: return {"status": "error", "message": f"No {index} data in DB. Update Weekly Expiry!"}

    if spot_ltp > 0:
        atm = round(spot_ltp / conf["Gap"]) * conf["Gap"]
        cursor.sort(key=lambda x: abs(x["Strike"] - atm))
    
    tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": exch_seg, "type": doc["Type"], "sym": doc["Symbol"], "strike": doc["Strike"]} for doc in cursor]
    
    best_ce = None
    best_pe = None
    
    for i in range(0, len(tokens_req), 50):
        batch = tokens_req[i:i+50]
        try:
            req_batch = [{"instrument_token": b["instrument_token"], "exchange_segment": b["exchange_segment"]} for b in batch]
            raw = client.quotes(instrument_tokens=req_batch, quote_type="all")
            raw_data = raw if isinstance(raw, list) else raw.get('data', [])
            
            ltp_map = {}
            for item in raw_data:
                tk = str(item.get('exchange_token', item.get('tk')))
                ltp_map[tk] = {"ltp": float(item.get('ltp', 0)), "oi": float(item.get('open_int', item.get('oi', 0)))}

            for b in batch:
                tk = b["instrument_token"]
                if tk in ltp_map:
                    ltp = ltp_map[tk]["ltp"]
                    oi = ltp_map[tk]["oi"]
                    
                    if 0 < ltp <= target_premium:
                        if b["type"] == "CE" and not best_ce: best_ce = {"sym": b["sym"], "tk": tk, "ltp": ltp, "oi": oi}
                        elif b["type"] == "PE" and not best_pe: best_pe = {"sym": b["sym"], "tk": tk, "ltp": ltp, "oi": oi}
                            
            if best_ce and best_pe: break
                
        except Exception as e: pass

    if not best_ce or not best_pe: return {"status": "error", "message": f"Could not find {index} CE/PE below ₹{target_premium}"}

    ce_ltp, pe_ltp = round_to_tick(best_ce["ltp"]), round_to_tick(best_pe["ltp"])
    ce_sl_trigger = round_to_tick(ce_ltp * (1 + sl_pct/100))
    pe_sl_trigger = round_to_tick(pe_ltp * (1 + sl_pct/100))
    ce_sl_limit = round_to_tick(ce_sl_trigger + 10.0)
    pe_sl_limit = round_to_tick(pe_sl_trigger + 10.0)

    if mode == "REAL":
        try:
            uniq = str(int(time.time()))[-6:]
            
            def fire_order(order_name, **kwargs):
                resp = client.place_order(**kwargs)
                if not resp:
                    raise Exception(f"{order_name} failed: Empty response from Kotak API.")
                
                if isinstance(resp, dict):
                    if resp.get("stat") == "Not_Ok" or resp.get("stat") == "Rejected":
                        err_msg = resp.get("errMsg", resp.get("message", "Order rejected by broker."))
                        raise Exception(f"{order_name} Rejected: {err_msg}")
                    if "nOrdNo" not in resp and "nOrdNo" not in resp.get("data", {}):
                        if resp.get("stat") != "Ok":
                            raise Exception(f"{order_name} Error: No Order ID generated. {str(resp)}")
                return resp

            ce_entry_price = round_to_tick(max(ce_ltp - 3.0, 0.5))
            pe_entry_price = round_to_tick(max(pe_ltp - 3.0, 0.5))

            entry_tasks = [
                asyncio.to_thread(fire_order, "CE Ent", exchange_segment=exch_seg, product="NRML", price=str(ce_entry_price), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"ce_e_{uniq}"),
                asyncio.to_thread(fire_order, "PE Ent", exchange_segment=exch_seg, product="NRML", price=str(pe_entry_price), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"pe_e_{uniq}")
            ]
            await asyncio.gather(*entry_tasks)

            sl_tasks = [
                asyncio.to_thread(fire_order, "CE SL", exchange_segment=exch_seg, product="NRML", price=str(ce_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(ce_sl_trigger), tag=f"ce_s_{uniq}"),
                asyncio.to_thread(fire_order, "PE SL", exchange_segment=exch_seg, product="NRML", price=str(pe_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(pe_sl_trigger), tag=f"pe_s_{uniq}")
            ]
            await asyncio.gather(*sl_tasks)

        except Exception as e: 
            return {"status": "error", "message": f"Kotak Trade Failed: {str(e)}"}
            
    db_col = get_collection("real_trades")
    trade_docs = [
        {"user_id": user_id, "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_ce["sym"], "token": best_ce["tk"], "transaction": "S", "qty": int(qty), "entry_price": ce_ltp, "ltp": ce_ltp, "entry_oi": best_ce.get("oi", 0), "sl": ce_sl_trigger, "status": "OPEN", "exch_seg": exch_seg}]},
        {"user_id": user_id, "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_pe["sym"], "token": best_pe["tk"], "transaction": "S", "qty": int(qty), "entry_price": pe_ltp, "ltp": pe_ltp, "entry_oi": best_pe.get("oi", 0), "sl": pe_sl_trigger, "status": "OPEN", "exch_seg": exch_seg}]}
    ]
    await db_col.insert_many(trade_docs)

    t_msg = f"🚀 <b>ALGO ORDER EXECUTED!</b>\n\n📈 <b>Index:</b> {index}\n🟢 <b>CE Leg:</b> {best_ce['sym']} @ ₹{ce_ltp}\n🔴 <b>PE Leg:</b> {best_pe['sym']} @ ₹{pe_ltp}\n📦 <b>Qty:</b> {qty} (Lots: {lots})\n🛡️ <b>SL:</b> {sl_pct}%\n\n⚡ <i>Mode: {mode}</i>"
    asyncio.create_task(send_user_alert(user_id, t_msg))
    return {"status": "success", "message": f"Orders Placed: {best_ce['sym']} & {best_pe['sym']}"}

@router.post("/execute-now")
async def manual_trigger_algo(mode: str = "PAPER", strat_id: str = None, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    strat_to_exec = None
    day = datetime.now(IST).weekday()
    
    if strat_id and state:
        for s in state.get("custom_strategies", []):
            if s["id"] == strat_id:
                strat_to_exec = s
                break
    elif state:
        found = False
        for s in state.get("custom_strategies", []):
            if s.get("is_active", True) and day in s.get("active_days", []):
                strat_to_exec = s
                found = True
                break
        if not found and state.get("default_active", True):
            strat_to_exec = None 
                
    try: return await core_algo_execution(current_user["id"], mode, strategy=strat_to_exec)
    except Exception as e: return {"status": "error", "message": str(e)}

# --- MARKET INTELLIGENCE ROUTES ---
@router.post("/toggle-total-alert")
async def toggle_total_alert(config: TotalAlertConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    await user_col.update_one({"id": current_user["id"]}, {"$set": {"total_alerts": {"is_active": config.is_active, "index": config.index, "last_sent_time": None, "min_frequency": 15}}}, upsert=True)
    if config.is_active: asyncio.create_task(send_user_alert(current_user["id"], f"✅ Total Analysis Alert ON for {config.index}"))
    return {"status": "success"}

@router.post("/toggle-custom-alert")
async def toggle_custom_alert(config: CustomStrikeConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    final_freq = max(3, config.frequency_minutes)
    await user_col.update_one({"id": current_user["id"]}, {"$set": {"custom_alerts": {"is_active": config.is_active, "index": config.index, "strikes": config.strikes, "frequency_minutes": final_freq, "last_sent_time": None}}}, upsert=True)
    if config.is_active: asyncio.create_task(send_user_alert(current_user["id"], f"✅ Custom Strike Alert ON for {config.index}"))
    return {"status": "success"}

@router.get("/market-intelligence")
async def get_market_intelligence(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
        conf = INDICES_CONFIG.get(symbol)
        q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
        spot_ltp = float(item.get('ltp', 0))
        if spot_ltp == 0 and symbol == "SENSEX":
            q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
            item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
            spot_ltp = float(item.get('ltp', 0))
        gap = conf["Gap"]
        atm = round(spot_ltp / gap) * gap

        coll = get_collection(conf["Coll"])
        cursor = await coll.find().to_list(length=None)
        if not cursor: raise Exception("Weekly data missing. Please Update Expiry.")

        strike_map = {}
        for doc in cursor:
            stk = doc["Strike"]
            if stk not in strike_map: strike_map[stk] = {"strike": stk, "ce_token": None, "pe_token": None, "ce_oi": 0, "pe_oi": 0, "ce_chg": 0, "pe_chg": 0}
            if doc["Type"] == "CE": strike_map[stk]["ce_token"] = doc["Token"]
            else: strike_map[stk]["pe_token"] = doc["Token"]

        all_strikes = sorted(strike_map.keys())
        req_tokens = []
        for stk in all_strikes:
            if strike_map[stk].get("ce_token"): req_tokens.append({"instrument_token": strike_map[stk]["ce_token"], "exchange_segment": conf["Exchange"]})
            if strike_map[stk].get("pe_token"): req_tokens.append({"instrument_token": strike_map[stk]["pe_token"], "exchange_segment": conf["Exchange"]})

        ce_tot_oi = pe_tot_oi = ce_tot_chg = pe_tot_chg = 0
        for i in range(0, len(req_tokens), 50):
            batch = req_tokens[i:i+50]
            try:
                q = client.quotes(instrument_tokens=batch, quote_type="all")
                raw = q if isinstance(q, list) else q.get('data', [])
                for item in raw:
                    tk = str(item.get('exchange_token') or item.get('tk'))
                    oi = float(item.get('open_int', 0))
                    oi_chg = float(item.get('change', 0)) 
                    for stk in all_strikes:
                        if strike_map[stk].get("ce_token") == tk:
                            strike_map[stk].update({"ce_oi": oi, "ce_chg": oi_chg})
                            ce_tot_oi += oi; ce_tot_chg += oi_chg
                        elif strike_map[stk].get("pe_token") == tk:
                            strike_map[stk].update({"pe_oi": oi, "pe_chg": oi_chg})
                            pe_tot_oi += oi; pe_tot_chg += oi_chg
            except: pass

        valid_strikes = [s for s in strike_map.values() if s["ce_oi"] > 0 or s["pe_oi"] > 0]
        above_atm = [s for s in valid_strikes if s["strike"] > atm]
        resistances = sorted(above_atm, key=lambda x: x["ce_oi"], reverse=True)[:3]
        resistances = sorted(resistances, key=lambda x: x["strike"]) 
        below_atm = [s for s in valid_strikes if s["strike"] < atm]
        supports = sorted(below_atm, key=lambda x: x["pe_oi"], reverse=True)[:3]
        supports = sorted(supports, key=lambda x: x["strike"], reverse=True) 

        record_snapshot(symbol, ce_tot_oi, pe_tot_oi)
        ce_1h_chg, pe_1h_chg, mins_passed = get_1h_change(symbol, ce_tot_oi, pe_tot_oi)
        max_pain = calculate_max_pain(valid_strikes)
        pcr = round(pe_tot_oi / ce_tot_oi, 2) if ce_tot_oi > 0 else 0
        trend = "BULLISH 🚀" if pcr >= 1.1 else "BEARISH 🩸" if pcr <= 0.9 else "SIDEWAYS ⚖️"
        diff_oi = pe_tot_oi - ce_tot_oi

        return {
            "status": "success", "spot": spot_ltp, "maxPain": max_pain, "pcr": pcr, "trend": trend,
            "ceTotalOi": format_oi(ce_tot_oi), "peTotalOi": format_oi(pe_tot_oi),
            "ceDayChange": format_oi(ce_tot_chg), "peDayChange": format_oi(pe_tot_chg),
            "ce1HourChange": format_oi(ce_1h_chg), "pe1HourChange": format_oi(pe_1h_chg),
            "historyMins": mins_passed, "difference": f"PE Dominating by {format_oi(abs(diff_oi))}" if diff_oi > 0 else f"CE Dominating by {format_oi(abs(diff_oi))}",
            "supports": [s["strike"] for s in supports], "resistances": [s["strike"] for s in resistances],
            "raw_strikes": strike_map, "lastUpdated": datetime.now(IST).strftime("%H:%M:%S")
        }
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

# --- SCHEDULERS ---
async def automatic_algo_scheduler():
    while True:
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")
        algo_col = get_collection("algo_state")
        db_col = get_collection("real_trades")
        
        all_users = await algo_col.find({}).to_list(length=None)
        for user_state in all_users:
            user_id = user_state["user_id"]
            
            if not user_state.get("is_active", False):
                continue
            
            if user_state.get("default_active", True) and user_state.get("last_executed_date") != current_date:
                if current_time == "10:00":
                    lock = await algo_col.update_one({"_id": user_state["_id"], "last_executed_date": user_state.get("last_executed_date")}, {"$set": {"last_executed_date": current_date}})
                    if lock.modified_count > 0:
                        try: await core_algo_execution(user_id, "REAL", strategy=None)
                        except: pass
            
            custom_strats = user_state.get("custom_strategies", [])
            for i, strat in enumerate(custom_strats):
                if strat.get("is_active", True) and strat.get("last_executed_date") != current_date:
                    if strat.get("entry_time") == current_time:
                        lock = await algo_col.update_one({"_id": user_state["_id"], f"custom_strategies.{i}.last_executed_date": strat.get("last_executed_date")}, {"$set": {f"custom_strategies.{i}.last_executed_date": current_date}})
                        if lock.modified_count > 0:
                            try: await core_algo_execution(user_id, "REAL", strategy=strat)
                            except: pass

        # 🚀 3:15 PM AUTO EXIT RE-WRITE WITH LIVE KOTAK POSITIONS (NO DB LAFDA)
        if current_time == "15:15":
            all_users = await algo_col.find({"is_active": True}).to_list(length=None)
            
            for user_state in all_users:
                user_id = user_state["user_id"]
                try:
                    client = get_kotak_client(user_id)
                    
                    # STEP 1: Pehle saare pending Stoploss/Limit orders cancel karo
                    try: 
                        order_report = client.order_report()
                        orders = order_report if isinstance(order_report, list) else order_report.get("data", [])
                        for ord_data in orders:
                            trd_sym = str(ord_data.get("trdSym", "")).upper()
                            
                            # 🚀 SAFETY FILTER: Sirf Index Options ke orders cancel honge
                            is_index_option = any(idx in trd_sym for idx in ["NIFTY", "SENSEX", "BANKNIFTY"]) and ("CE" in trd_sym or "PE" in trd_sym)
                            if not is_index_option:
                                continue
                                
                            status = str(ord_data.get("ordSt", "")).lower()
                            if status in ["opn", "trg", "pending", "trigger pending", "open", "put", "modified"]:
                                try: client.cancel_order(nOrdNo=str(ord_data.get("nOrdNo")))
                                except: pass
                    except: pass
                        
                    # STEP 2: Live Positions uthao aur MKT Exit maaro
                    try:
                        pos_response = client.positions()
                        if isinstance(pos_response, dict) and "data" in pos_response:
                            pos_data = pos_response["data"]
                        elif isinstance(pos_response, list):
                            pos_data = pos_response
                        else:
                            pos_data = []
                            
                        exited_something = False
                        
                        for p in pos_data:
                            trd_sym = str(p.get("trdSym", "")).upper()
                            
                            # 🚀 SAFETY FILTER: Sirf Index Options ki positions square off hongi!
                            is_index_option = any(idx in trd_sym for idx in ["NIFTY", "SENSEX", "BANKNIFTY"]) and ("CE" in trd_sym or "PE" in trd_sym)
                            if not is_index_option:
                                continue  # Equity, ETF, ya kisi aur stock ko skip kar do
                                
                            # String to safe integer conversion
                            buy_qty = int(float(p.get("flBuyQty", p.get("buyQty", 0))))
                            sell_qty = int(float(p.get("flSellQty", p.get("sellQty", 0))))
                            net_qty = buy_qty - sell_qty
                            
                            # Agar position bachi hai (SL hit nahi hua) tabhi Exit maarega
                            if net_qty != 0:
                                exit_trans = "S" if net_qty > 0 else "B" 
                                abs_qty = abs(net_qty)
                                
                                try:
                                    client.place_order(
                                        exchange_segment=p.get("exSeg", "nse_fo"), product="NRML", price="0", 
                                        order_type="MKT", quantity=str(abs_qty), validity="DAY", 
                                        trading_symbol=p.get("trdSym"), transaction_type=exit_trans, amo="NO"
                                    )
                                    exited_something = True
                                except Exception as e:
                                    print(f"Auto Exit Error on {p.get('trdSym')}: {e}")
                                    
                        if exited_something:
                            asyncio.create_task(send_user_alert(user_id, f"⏰ <b>AUTO EXIT @ 15:15</b>\n\n✅ Live options squared off successfully."))
                            
                    except Exception as e:
                        pass
                        
                    # STEP 3: Last mein sirf history clear karne ke liye DB update
                    await db_col.update_many(
                        {"user_id": user_id, "status": "OPEN"}, 
                        {"$set": {"status": "CLOSED"}}
                    )
                    
                except: pass

        await asyncio.sleep(61 - datetime.now(IST).second)

async def telegram_intelligence_scheduler():
    while True:
        now_dt = datetime.now(IST)
        now_time = now_dt.time()
        if datetime.strptime("09:15", "%H:%M").time() <= now_time <= datetime.strptime("15:30", "%H:%M").time():
            try:
                user_col = get_collection("users")
                active_users = await user_col.find({"$or": [{"total_alerts.is_active": True}, {"custom_alerts.is_active": True}]}).to_list(length=None)
                for user in active_users:
                    total_conf = user.get("total_alerts", {})
                    if total_conf.get("is_active") == True:
                        last_total = total_conf.get("last_sent_time")
                        if not last_total or (now_dt - datetime.fromisoformat(last_total)).total_seconds() >= 15 * 60:
                            lock = await user_col.update_one({"_id": user["_id"], "total_alerts.last_sent_time": last_total}, {"$set": {"total_alerts.last_sent_time": now_dt.isoformat()}})
                            if lock.modified_count > 0:
                                idx = total_conf.get("index", "NIFTY")
                                try:
                                    data = await get_market_intelligence(symbol=idx, current_user={"id": user["id"]})
                                    if data.get("status") == "success":
                                        s, r = data["supports"], data["resistances"]
                                        t_msg = f"📊 <b>{idx} MASTER PREDICTION</b>\n\n🎯 <b>Spot:</b> {data['spot']}\n⚖️ <b>Max Pain:</b> {data['maxPain']}\n📈 <b>PCR:</b> {data['pcr']} ({data['trend']})\n\n🛡️ <b>KEY LEVELS</b>\n🔺 <b>R3:</b> {r[2] if len(r)>2 else '-'} | <b>R2:</b> {r[1] if len(r)>1 else '-'} | <b>R1:</b> {r[0] if len(r)>0 else '-'}\n🔻 <b>S1:</b> {s[0] if len(s)>0 else '-'} | <b>S2:</b> {s[1] if len(s)>1 else '-'} | <b>S3:</b> {s[2] if len(s)>2 else '-'}\n\n📅 <b>DAY OPEN INTEREST (All Strikes)</b>\n🔴 <b>CE OI:</b> {data['ceTotalOi']} (Added: {data['ceDayChange']})\n🟢 <b>PE OI:</b> {data['peTotalOi']} (Added: {data['peDayChange']})\n⚔️ <b>Status:</b> {data['difference']}\n\n⏳ <b>LAST {data['historyMins']} MINS ACTIVITY</b>\n🔴 <b>CE Change:</b> {data['ce1HourChange']}\n🟢 <b>PE Change:</b> {data['pe1HourChange']}"
                                        await send_user_alert(user["id"], t_msg)
                                except: pass
                    cust_conf = user.get("custom_alerts", {})
                    if cust_conf.get("is_active") == True:
                        last_cust = cust_conf.get("last_sent_time")
                        freq = max(3, cust_conf.get("frequency_minutes", 3))
                        if not last_cust or (now_dt - datetime.fromisoformat(last_cust)).total_seconds() >= freq * 60:
                            lock = await user_col.update_one({"_id": user["_id"], "custom_alerts.last_sent_time": last_cust}, {"$set": {"custom_alerts.last_sent_time": now_dt.isoformat()}})
                            if lock.modified_count > 0:
                                idx = cust_conf.get("index", "NIFTY")
                                target_strikes = cust_conf.get("strikes", [])
                                try:
                                    data = await get_market_intelligence(symbol=idx, current_user={"id": user["id"]})
                                    if data.get("status") == "success":
                                        raw_map = data["raw_strikes"]
                                        c_msg = f"🎯 <b>{idx} CUSTOM STRIKES UPDATE</b>\n⏱️ <b>Spot:</b> {data['spot']}\n\n"
                                        for stk in target_strikes:
                                            if stk in raw_map:
                                                s_data = raw_map[stk]
                                                c_msg += f"⚡ <b>Strike: {stk}</b>\n🔴 CE OI: {format_oi(s_data['ce_oi'])} (Chg: {format_oi(s_data['ce_chg'])})\n🟢 PE OI: {format_oi(s_data['pe_oi'])} (Chg: {format_oi(s_data['pe_chg'])})\n\n"
                                        await send_user_alert(user["id"], c_msg.strip())
                                except: pass
            except: pass
        await asyncio.sleep(60)

@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(automatic_algo_scheduler())
    asyncio.create_task(telegram_intelligence_scheduler())
