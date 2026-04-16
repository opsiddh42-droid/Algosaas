import os
import asyncio
import httpx
import time
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
class AlgoConfig(BaseModel):
    use_default: bool
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float
    active_days: List[int]

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
    
    default_config = {"use_default": True, "index": "NIFTY", "entry_time": "10:00", "max_premium": 10.0, "sl_pct": 200.0, "active_days": []}
    if not state:
        state = {"user_id": current_user["id"], "is_active": False, "last_executed_date": "", "config": default_config}
        await algo_col.insert_one(state)
        
    config = state.get("config", default_config)
    
    if config.get("use_default", True):
        day = datetime.now(IST).weekday()
        if day in [0, 1, 4]: plan = "Default: NIFTY Sell @ ₹10"
        elif day in [2, 3]: plan = "Default: SENSEX Sell @ ₹15"
        else: plan = "Default: Idle Today"
    else:
        plan = f"Custom: {config.get('index', 'NIFTY')} Sell <= ₹{config.get('max_premium', 10)}"

    return {
        "status": "success", "is_active": state.get("is_active", False),
        "plan_today": plan, "config": config,
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

async def core_algo_execution(user_id: str, mode: str):
    now = datetime.now(IST)
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": user_id})
    config = state.get("config", {"use_default": True}) if state else {"use_default": True}
    day = now.weekday()

    if config.get("use_default", True):
        if day in [0, 1, 4]:
            index, target_premium, sl_pct = "NIFTY", 10.0, 200.0
        elif day in [2, 3]:
            index, target_premium, sl_pct = "SENSEX", 15.0, 200.0
        else:
            return {"status": "error", "message": "No default strategy planned for today."}
    else:
        if day not in config.get("active_days", []): return {"status": "error", "message": "Custom strategy is not configured to run today."}
        index = config.get("index", "NIFTY")
        target_premium, sl_pct = float(config.get("max_premium", 10.0)), float(config.get("sl_pct", 200.0))

    if index == "NIFTY": qty = "65"
    elif index == "BANKNIFTY": qty = "15"
    elif index == "SENSEX": qty = "20"
    else: qty = "65"

    try: client = get_kotak_client(user_id)
    except HTTPException: return {"status": "error", "message": "Kotak Session OFF! Please Login."}

    # 🚀 STEP 1: GET SPOT PRICE TO FIND ATM
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

    # 🚀 STEP 2: SORT STRIKES FROM ATM OUTWARDS
    if spot_ltp > 0:
        atm = round(spot_ltp / conf["Gap"]) * conf["Gap"]
        cursor.sort(key=lambda x: abs(x["Strike"] - atm)) # Sort: Closest to ATM first
    
    tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": exch_seg, "type": doc["Type"], "sym": doc["Symbol"], "strike": doc["Strike"]} for doc in cursor]
    
    best_ce = None
    best_pe = None
    
    # 🚀 STEP 3: FETCH IN BATCHES AND STOP EARLY
    for i in range(0, len(tokens_req), 50):
        batch = tokens_req[i:i+50]
        try:
            req_batch = [{"instrument_token": b["instrument_token"], "exchange_segment": b["exchange_segment"]} for b in batch]
            raw = client.quotes(instrument_tokens=req_batch, quote_type="all")
            raw_data = raw if isinstance(raw, list) else raw.get('data', [])
            
            # Map LTP for quick lookup
            ltp_map = {}
            for item in raw_data:
                tk = str(item.get('exchange_token', item.get('tk')))
                ltp_map[tk] = {
                    "ltp": float(item.get('ltp', 0)),
                    "oi": float(item.get('open_int', item.get('oi', 0)))
                }

            # Check prices moving outwards from ATM
            for b in batch:
                tk = b["instrument_token"]
                if tk in ltp_map:
                    ltp = ltp_map[tk]["ltp"]
                    oi = ltp_map[tk]["oi"]
                    
                    if 0 < ltp <= target_premium:
                        if b["type"] == "CE" and not best_ce:
                            best_ce = {"sym": b["sym"], "tk": tk, "ltp": ltp, "oi": oi}
                        elif b["type"] == "PE" and not best_pe:
                            best_pe = {"sym": b["sym"], "tk": tk, "ltp": ltp, "oi": oi}
                            
            # 🚀 EARLY EXIT: Stop API calls if both CE & PE are found
            if best_ce and best_pe:
                break
                
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
                if isinstance(resp, dict) and resp.get("stat") != "Ok": 
                    raise Exception(f"{order_name} Error: " + str(resp))
                return resp

            # 🚀 SMART LIMIT ORDER
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

        except Exception as e: return {"status": "error", "message": f"Kotak Error: {str(e)}"}
            
    db_col = get_collection("real_trades")
    trade_docs = [
        {"user_id": user_id, "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_ce["sym"], "token": best_ce["tk"], "transaction": "S", "qty": int(qty), "entry_price": ce_ltp, "ltp": ce_ltp, "entry_oi": best_ce.get("oi", 0), "sl": ce_sl_trigger, "status": "OPEN", "exch_seg": exch_seg}]},
        {"user_id": user_id, "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_pe["sym"], "token": best_pe["tk"], "transaction": "S", "qty": int(qty), "entry_price": pe_ltp, "ltp": pe_ltp, "entry_oi": best_pe.get("oi", 0), "sl": pe_sl_trigger, "status": "OPEN", "exch_seg": exch_seg}]}
    ]
    await db_col.insert_many(trade_docs)
    await algo_col.update_one({"user_id": user_id}, {"$set": {"last_executed_date": now.strftime("%Y-%m-%d")}})

    if mode == "REAL":
        t_msg = f"🚀 <b>ALGO ORDER EXECUTED!</b>\n\n📈 <b>Index:</b> {index}\n🟢 <b>CE Leg:</b> {best_ce['sym']} @ ₹{ce_ltp}\n🔴 <b>PE Leg:</b> {best_pe['sym']} @ ₹{pe_ltp}\n📦 <b>Qty:</b> {qty}\n🛡️ <b>SL:</b> {sl_pct}%\n\n⚡ <i>Orders successfully placed!</i>"
        asyncio.create_task(send_user_alert(user_id, t_msg))
    return {"status": "success", "message": f"Orders Placed: {best_ce['sym']} & {best_pe['sym']}"}

@router.post("/execute-now")
async def manual_trigger_algo(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    try: return await core_algo_execution(current_user["id"], mode)
    except Exception as e: return {"status": "error", "message": str(e)}

# --- MARKET INTELLIGENCE ROUTES ---
@router.post("/toggle-total-alert")
async def toggle_total_alert(config: TotalAlertConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    await user_col.update_one(
        {"id": current_user["id"]}, 
        {"$set": {"total_alerts": {
            "is_active": config.is_active, 
            "index": config.index, 
            "last_sent_time": None,
            "min_frequency": 15
        }}},
        upsert=True
    )
    if config.is_active:
        msg = f"✅ <b>Total Analysis Alert ON!</b>\nIndex: {config.index}\nFrequency: Every 15 Mins\nIncludes: All Strikes OI, 1H Change, Support & Resistance."
        asyncio.create_task(send_user_alert(current_user["id"], msg))
    return {"status": "success", "message": "Total Analysis Alert updated (15m Limit)."}

@router.post("/toggle-custom-alert")
async def toggle_custom_alert(config: CustomStrikeConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    final_freq = max(3, config.frequency_minutes)
    
    await user_col.update_one(
        {"id": current_user["id"]}, 
        {"$set": {"custom_alerts": {
            "is_active": config.is_active, 
            "index": config.index, 
            "strikes": config.strikes, 
            "frequency_minutes": final_freq,
            "last_sent_time": None
        }}},
        upsert=True
    )
    if config.is_active:
        strikes_str = ", ".join(map(str, config.strikes))
        msg = f"✅ <b>Custom Strike Alert ON!</b>\nIndex: {config.index}\nStrikes: {strikes_str}\nFrequency: Every {final_freq} Mins."
        asyncio.create_task(send_user_alert(current_user["id"], msg))
    return {"status": "success", "message": f"Custom Strike Alert updated ({final_freq}m Limit)."}


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
            "status": "success",
            "spot": spot_ltp,
            "maxPain": max_pain,
            "pcr": pcr,
            "trend": trend,
            "ceTotalOi": format_oi(ce_tot_oi),
            "peTotalOi": format_oi(pe_tot_oi),
            "ceDayChange": format_oi(ce_tot_chg),
            "peDayChange": format_oi(pe_tot_chg),
            "ce1HourChange": format_oi(ce_1h_chg),
            "pe1HourChange": format_oi(pe_1h_chg),
            "historyMins": mins_passed,
            "difference": f"PE Dominating by {format_oi(abs(diff_oi))}" if diff_oi > 0 else f"CE Dominating by {format_oi(abs(diff_oi))}",
            "supports": [s["strike"] for s in supports],
            "resistances": [s["strike"] for s in resistances],
            "raw_strikes": strike_map, 
            "lastUpdated": datetime.now(IST).strftime("%H:%M:%S")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- SCHEDULER ---
async def automatic_algo_scheduler():
    while True:
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")

        algo_col = get_collection("algo_state")
        db_col = get_collection("real_trades")
        
        # 🟢 1. CHECK FOR ENTRY (WITH ATOMIC LOCK)
        active_users = await algo_col.find({"is_active": True, "last_executed_date": {"$ne": current_date}}).to_list(length=None)
        for user_state in active_users:
            if user_state.get("config", {}).get("entry_time", "10:00") == current_time:
                # 🚀 ATOMIC LOCK: Prevents multiple workers from firing the same order
                lock_result = await algo_col.update_one(
                    {"_id": user_state["_id"], "last_executed_date": user_state.get("last_executed_date")},
                    {"$set": {"last_executed_date": current_date}}
                )
                if lock_result.modified_count == 0:
                    continue # Another worker already picked this up
                
                try: await core_algo_execution(user_state["user_id"], "REAL")
                except: pass

        # 🟢 2. CHECK FOR 3:15 PM AUTO EXIT & SL CANCEL (ALREADY HAS ATOMIC LOCK)
        if current_time == "15:15":
            open_trades = await db_col.find({"status": "OPEN"}).to_list(length=None)
            for trade in open_trades:
                lock_result = await db_col.update_one(
                    {"_id": trade["_id"], "status": "OPEN"}, 
                    {"$set": {"status": "PROCESSING"}} 
                )
                if lock_result.modified_count == 0: continue
                
                user_id = trade["user_id"]
                try:
                    client = get_kotak_client(user_id)
                    try:
                        order_report = client.order_report()
                        orders = order_report if isinstance(order_report, list) else order_report.get("data", [])
                    except Exception: orders = []

                    updated_legs = []
                    for leg in trade["legs"]:
                        if leg["status"] == "OPEN":
                            for ord_data in orders:
                                status = str(ord_data.get("ordSt", "")).lower()
                                sym = ord_data.get("trdSym", "")
                                ord_no = ord_data.get("nOrdNo", "")
                                if sym == leg["symbol"] and status in ["opn", "trg", "pending", "trigger pending", "open", "put", "modified"]:
                                    try: client.cancel_order(nOrdNo=str(ord_no))
                                    except Exception as ce: pass

                            exit_trans = "B" if leg["transaction"] == "S" else "S"
                            try:
                                client.place_order(exchange_segment=leg.get("exch_seg", "nse_fo"), product="NRML", price="0", order_type="MKT", quantity=str(leg["qty"]), validity="DAY", trading_symbol=leg["symbol"], transaction_type=exit_trans, amo="NO")
                                leg["status"] = "CLOSED"
                            except Exception as e: pass
                                
                        updated_legs.append(leg)
                    
                    await db_col.update_one({"_id": trade["_id"]}, {"$set": {"status": "CLOSED", "legs": updated_legs}})
                    asyncio.create_task(send_user_alert(user_id, f"⏰ <b>AUTO EXIT @ 15:15</b>\n\n✅ Pending SL Canceled\n✅ Positions squared off automatically."))
                except Exception as e: pass

        sleep_sec = 61 - datetime.now(IST).second
        await asyncio.sleep(sleep_sec)

async def telegram_intelligence_scheduler():
    while True:
        now_dt = datetime.now(IST)
        now_time = now_dt.time()
        
        # Run only during market hours
        if datetime.strptime("09:15", "%H:%M").time() <= now_time <= datetime.strptime("15:30", "%H:%M").time():
            try:
                user_col = get_collection("users")
                active_users = await user_col.find({
                    "$or": [{"total_alerts.is_active": True}, {"custom_alerts.is_active": True}]
                }).to_list(length=None)
                
                for user in active_users:
                    # 1️⃣ TOTAL ANALYSIS (WITH ATOMIC LOCK)
                    total_conf = user.get("total_alerts", {})
                    if total_conf.get("is_active"):
                        last_total = total_conf.get("last_sent_time")
                        if not last_total or (now_dt - datetime.fromisoformat(last_total)).total_seconds() >= 15 * 60:
                            
                            # 🚀 ATOMIC LOCK: Prevents multiple workers from sending the same Telegram message
                            lock = await user_col.update_one(
                                {"_id": user["_id"], "total_alerts.last_sent_time": last_total},
                                {"$set": {"total_alerts.last_sent_time": now_dt.isoformat()}}
                            )
                            if lock.modified_count == 0: continue
                            
                            idx = total_conf.get("index", "NIFTY")
                            try:
                                data = await get_market_intelligence(symbol=idx, current_user={"id": user["id"]})
                                if data.get("status") == "success":
                                    s, r = data["supports"], data["resistances"]
                                    t_msg = (
                                        f"📊 <b>{idx} MASTER PREDICTION</b>\n\n"
                                        f"🎯 <b>Spot:</b> {data['spot']}\n"
                                        f"📈 <b>PCR:</b> {data['pcr']} ({data['trend']})\n\n"
                                        f"🛡️ <b>KEY LEVELS</b>\n"
                                        f"🔺 <b>R3:</b> {r[2] if len(r)>2 else '-'} | <b>R2:</b> {r[1] if len(r)>1 else '-'} | <b>R1:</b> {r[0] if len(r)>0 else '-'}\n"
                                        f"🔻 <b>S1:</b> {s[0] if len(s)>0 else '-'} | <b>S2:</b> {s[1] if len(s)>1 else '-'} | <b>S3:</b> {s[2] if len(s)>2 else '-'}\n\n"
                                        f"📅 <b>DAY OI (All Strikes)</b>\n"
                                        f"🔴 <b>CE:</b> {data['ceTotalOi']} | 🟢 <b>PE:</b> {data['peTotalOi']}\n"
                                        f"⚔️ <b>Status:</b> {data['difference']}\n\n"
                                        f"⏳ <b>LAST {data['historyMins']} MINS CHANGE</b>\n"
                                        f"🔴 <b>CE:</b> {data['ce1HourChange']} | 🟢 <b>PE:</b> {data['pe1HourChange']}"
                                    )
                                    await send_user_alert(user["id"], t_msg)
                            except: pass

                    # 2️⃣ CUSTOM STRIKES (WITH ATOMIC LOCK)
                    cust_conf = user.get("custom_alerts", {})
                    if cust_conf.get("is_active"):
                        last_cust = cust_conf.get("last_sent_time")
                        freq = max(3, cust_conf.get("frequency_minutes", 3))
                        if not last_cust or (now_dt - datetime.fromisoformat(last_cust)).total_seconds() >= freq * 60:
                            
                            # 🚀 ATOMIC LOCK: Prevents multiple workers from sending the same custom Telegram message
                            lock = await user_col.update_one(
                                {"_id": user["_id"], "custom_alerts.last_sent_time": last_cust},
                                {"$set": {"custom_alerts.last_sent_time": now_dt.isoformat()}}
                            )
                            if lock.modified_count == 0: continue
                            
                            idx = cust_conf.get("index", "NIFTY")
                            target_strikes = cust_conf.get("strikes", [])
                            try:
                                data = await get_market_intelligence(symbol=idx, current_user={"id": user["id"]})
                                if data.get("status") == "success":
                                    raw_map = data["raw_strikes"]
                                    c_msg = f"🎯 <b>{idx} CUSTOM STRIKES</b>\n⏱️ <b>Spot:</b> {data['spot']}\n\n"
                                    for stk in target_strikes:
                                        if stk in raw_map:
                                            s_data = raw_map[stk]
                                            c_msg += f"⚡ <b>Strike: {stk}</b>\n"
                                            c_msg += f"🔴 CE: {format_oi(s_data['ce_oi'])} ({format_oi(s_data['ce_chg'])})\n"
                                            c_msg += f"🟢 PE: {format_oi(s_data['pe_oi'])} ({format_oi(s_data['pe_chg'])})\n\n"
                                    await send_user_alert(user["id"], c_msg.strip())
                            except: pass
            except: pass
        
        await asyncio.sleep(60)

@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(automatic_algo_scheduler())
    asyncio.create_task(telegram_intelligence_scheduler())
