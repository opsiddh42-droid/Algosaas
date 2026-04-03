import os
import time
import asyncio
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.api.v1.market import get_kotak_client
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

load_dotenv()

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

# 🟢 TELEGRAM SETUP SECTION 🟢
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "") 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

class AlgoConfig(BaseModel):
    use_default: bool
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float
    active_days: List[int]

# 🟢 Helper: Kotak Neo requires valid price ticks (multiples of 0.05)
def round_to_tick(price: float) -> float:
    return round(price * 20) / 20.0

# 🟢 HELPER: SEND TELEGRAM ALERT 🟢
async def send_user_alert(user_id: str, message: str):
    if not BOT_TOKEN: return
    try:
        user_col = get_collection("users")
        user = await user_col.find_one({"id": user_id})
        if user and user.get("telegram_chat_id"):
            chat_id = user["telegram_chat_id"]
            async with httpx.AsyncClient() as client:
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=5)
    except Exception as e:
        print(f"Telegram Alert Error: {e}")

# 🟢 WEBHOOK: AUTO-REGISTER USER UCC 🟢
@router.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        if "message" not in data:
            return {"status": "ok"}
            
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
        print("Telegram Webhook Error:", e)
        
    return {"status": "ok"}


@router.get("/status")
async def get_algo_status(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    
    default_config = {
        "use_default": True, 
        "index": "NIFTY", 
        "entry_time": "10:00", 
        "max_premium": 6.0, 
        "sl_pct": 200.0,
        "active_days": []
    }

    if not state:
        state = {"user_id": current_user["id"], "is_active": False, "last_executed_date": "", "config": default_config}
        await algo_col.insert_one(state)
        
    config = state.get("config", default_config)
    
    if config.get("use_default", True):
        day = datetime.now(IST).weekday()
        if day in [0, 4]: plan = "Default: NIFTY Sell @ ₹6"
        elif day == 1: plan = "Default: SENSEX Sell @ ₹12"
        else: plan = "Default: Idle Today"
    else:
        plan = f"Custom: {config.get('index', 'NIFTY')} Sell <= ₹{config.get('max_premium', 6)}"

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

    day = now.weekday()

    # 1. APPLY LOGIC (Days Check)
    if config.get("use_default", True):
        if day not in [0, 1, 4]: 
            return {"status": "error", "message": "No default strategy planned for today."}
        index = "SENSEX" if day == 1 else "NIFTY"
        target_premium = 12.0 if day == 1 else 6.0
        sl_pct = 200.0
    else:
        active_days = config.get("active_days", [])
        if day not in active_days:
            return {"status": "error", "message": "Custom strategy is not configured to run today."}
            
        index = config.get("index", "NIFTY")
        target_premium = float(config.get("max_premium", 6.0))
        sl_pct = float(config.get("sl_pct", 200.0))

    # Yahan lot sizes update kiye gaye hain
    qty = "20" if index == "SENSEX" else "20" if index == "BANKNIFTY" else "65"
    coll_name = f"{index.lower()}_strike_data"
    exch_seg = "bse_fo" if index == "SENSEX" else "nse_fo"

    # 2. GET LIVE DATA
    client = get_kotak_client(current_user["id"])
    coll = get_collection(coll_name)
    cursor = await coll.find().to_list(length=None)
    if not cursor:
        raise HTTPException(status_code=400, detail=f"No {index} data in DB. Please Update Weekly Expiry first!")

    tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": exch_seg} for doc in cursor]
    
    ce_list, pe_list = [] , []
    for i in range(0, len(tokens_req), 50):
        batch = tokens_req[i:i+50]
        try:
            q = client.quotes(instrument_tokens=batch, quote_type="all")
            raw = q if isinstance(q, list) else q.get('data', [])
            for item in raw:
                tk = str(item.get('exchange_token') or item.get('tk'))
                ltp = float(item.get('ltp', 0))
                oi = float(item.get('oi', 0)) # 🟢 CAPTURE OI HERE 🟢
                doc = next((d for d in cursor if d["Token"] == tk), None)
                if doc and ltp > 0:
                    if doc["Type"] == "CE": ce_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp, "oi": oi})
                    else: pe_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp, "oi": oi})
        except: pass

    # 3. FIND BEST PREMIUM MATCH
    ce_list.sort(key=lambda x: x["ltp"], reverse=True)
    pe_list.sort(key=lambda x: x["ltp"], reverse=True)

    best_ce = next((x for x in ce_list if x["ltp"] <= target_premium), None)
    best_pe = next((x for x in pe_list if x["ltp"] <= target_premium), None)

    if not best_ce or not best_pe:
        return {"status": "error", "message": f"Could not find {index} CE/PE below ₹{target_premium}"}

    # 4. CALCULATE TRIGGERS & LIMITS (10 Point Buffer with Tick Formatting)
    ce_ltp = round_to_tick(best_ce["ltp"])
    pe_ltp = round_to_tick(best_pe["ltp"])

    ce_sl_trigger = round_to_tick(ce_ltp * (1 + sl_pct/100))
    ce_sl_limit = round_to_tick(ce_sl_trigger + 10.0)

    pe_sl_trigger = round_to_tick(pe_ltp * (1 + sl_pct/100))
    pe_sl_limit = round_to_tick(pe_sl_trigger + 10.0)

    # 🟢 5. FIRE STRICT ORDERS TO KOTAK NEO (WITH RAW ERROR DUMP)
    if mode == "REAL":
        try:
            import time
            # Har order ke liye ek unique ID banayenge (timestamp ke aakhri 6 digit)
            uniq = str(int(time.time()))[-6:]

            def fire_order(tag_name, **kwargs):
                try:
                    resp = client.place_order(**kwargs)
                except Exception as api_err:
                    raise Exception(f"SDK Exception -> {str(api_err)}")
                
                if isinstance(resp, dict) and resp.get("stat") != "Ok":
                    raw_error = str(resp)
                    raise Exception(f"API Reject -> {raw_error}")
                
                return resp

            # -- ENTRY ORDERS (Limit Sell at current LTP) --
            fire_order("CE Entry", exchange_segment=exch_seg, product="NRML", price=str(ce_ltp), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", market_protection="0", pf="N", trigger_price="0", tag=f"ce_ent_{uniq}")
            
            fire_order("PE Entry", exchange_segment=exch_seg, product="NRML", price=str(pe_ltp), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", market_protection="0", pf="N", trigger_price="0", tag=f"pe_ent_{uniq}")

            # -- STOPLOSS ORDERS (Buy Limit with 10 pt buffer) --
            fire_order("CE SL", exchange_segment=exch_seg, product="NRML", price=str(ce_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", market_protection="0", pf="N", trigger_price=str(ce_sl_trigger), tag=f"ce_sl_{uniq}")
            
            fire_order("PE SL", exchange_segment=exch_seg, product="NRML", price=str(pe_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", market_protection="0", pf="N", trigger_price=str(pe_sl_trigger), tag=f"pe_sl_{uniq}")

        except Exception as e:
            return {"status": "error", "message": f"Kotak Error: {str(e)}"}
            
    # 6. SAVE TO DB FOR UI TRACKING (🟢 WITH ENTRY OI 🟢)
    db_col = get_collection("real_trades" if mode == "REAL" else "paper_trades")
    trade_docs = [
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_ce["sym"], "token": best_ce["tk"], "transaction": "S", "qty": int(qty), "entry_price": ce_ltp, "ltp": ce_ltp, "entry_oi": best_ce.get("oi", 0), "sl": ce_sl_trigger, "target": 0, "status": "OPEN"}]},
        {"user_id": current_user["id"], "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), "is_algo": True, "legs": [{"symbol": best_pe["sym"], "token": best_pe["tk"], "transaction": "S", "qty": int(qty), "entry_price": pe_ltp, "ltp": pe_ltp, "entry_oi": best_pe.get("oi", 0), "sl": pe_sl_trigger, "target": 0, "status": "OPEN"}]}
    ]
    await db_col.insert_many(trade_docs)
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"last_executed_date": now.strftime("%Y-%m-%d")}})

    # 🟢 7. SEND EXECUTION NOTIFICATION 🟢
    if mode == "REAL":
        t_msg = (
            f"🚀 <b>ALGO ORDER EXECUTED!</b>\n\n"
            f"📈 <b>Index:</b> {index}\n"
            f"🟢 <b>CE Leg:</b> {best_ce['sym']} @ ₹{ce_ltp}\n"
            f"🔴 <b>PE Leg:</b> {best_pe['sym']} @ ₹{pe_ltp}\n"
            f"📦 <b>Qty:</b> {qty}\n"
            f"🛡️ <b>Stoploss:</b> {sl_pct}%\n\n"
            f"⚡ <i>Orders successfully placed on Kotak Neo!</i>"
        )
        asyncio.create_task(send_user_alert(current_user["id"], t_msg))

    return {"status": "success", "message": f"Limit Orders Placed: {best_ce['sym']} & {best_pe['sym']} with strict SL."}

# 🟢 BACKGROUND OI & PRICE MONITOR (Runs every 3 mins) 🟢
async def oi_price_monitor():
    while True:
        await asyncio.sleep(180) # 3 Minute Wait
        try:
            db_col = get_collection("real_trades")
            open_trades = await db_col.find({"status": "OPEN", "is_algo": True}).to_list(length=None)
            if not open_trades: continue
                
            for trade in open_trades:
                user_id = trade["user_id"]
                try: client = get_kotak_client(user_id)
                except: continue
                
                for leg in trade["legs"]:
                    if leg["status"] != "OPEN": continue
                    
                    token = leg["token"]
                    sym = leg["symbol"]
                    entry_price = float(leg.get("entry_price", 0))
                    entry_oi = float(leg.get("entry_oi", 0))
                    
                    if entry_oi <= 0 or entry_price <= 0: continue
                    
                    entry_time = datetime.strptime(trade["entry_time"], "%Y-%m-%d %H:%M:%S")
                    minutes_elapsed = (datetime.now() - entry_time).total_seconds() / 60.0

                    try:
                        q = client.quotes(instrument_tokens=[{"instrument_token": token, "exchange_segment": "nse_fo"}], quote_type="all")
                        raw = q if isinstance(q, list) else q.get('data', [])
                        live_data = next((item for item in raw if str(item.get('exchange_token') or item.get('tk')) == token), None)
                        if not live_data: continue
                        
                        current_ltp = float(live_data.get('ltp', 0))
                        current_oi = float(live_data.get('oi', 0))
                        
                        price_change_pct = ((current_ltp - entry_price) / entry_price) * 100
                        oi_change_pct = ((current_oi - entry_oi) / entry_oi) * 100
                        
                        msg = ""
                        alert_priority = "Normal"

                        # 1. TIME SPEED FILTER
                        if abs(oi_change_pct) > 10.0 and minutes_elapsed <= 10.0:
                            alert_priority = "FAST MOVE ⚡"

                        # 2. SMART CONDITIONS
                        if current_oi < entry_oi and current_ltp > entry_price:
                            msg = "🚨 Unwinding - High Danger! (OI Decreasing, Price Increasing)"
                        elif current_oi > entry_oi and current_ltp > entry_price:
                            msg = "⚠️ Short Covering - Risk Increasing (OI Increasing, Price Increasing)"
                        elif current_oi > entry_oi and current_ltp <= entry_price:
                            msg = "✅ Writers Active - Safe (OI Increasing, Price Stable/Down)"
                            
                        # 3. 3-LEVEL WARNING
                        if oi_change_pct > 40.0 and price_change_pct > 50.0:
                            msg = "💥 LEVEL 3: EXIT NOW! (Strongest Danger Alert)"
                        elif oi_change_pct > 25.0 and price_change_pct > 40.0:
                            msg = "🔥 LEVEL 2: Strong Danger (Consider 50% Exit)"
                        elif oi_change_pct > 15.0 and price_change_pct > 25.0:
                            msg = "⚠️ LEVEL 1: Early Warning (Consider Tightening SL)"
                            
                        if msg:
                            alert_text = (f"🤖 <b>ALGO MONITOR [{alert_priority}]</b>\n\n📊 <b>Symbol:</b> {sym}\n💰 <b>LTP:</b> ₹{current_ltp} ({'+' if price_change_pct>0 else ''}{price_change_pct:.1f}%)\n📈 <b>OI Change:</b> {'+' if oi_change_pct>0 else ''}{oi_change_pct:.1f}%\n⏳ <b>Time:</b> {int(minutes_elapsed)} mins\n\n🛑 <b>Alert:</b> <b>{msg}</b>\n\n<i>(No action taken by bot. Manual check advised.)</i>")
                            asyncio.create_task(send_user_alert(user_id, alert_text))
                    except Exception: pass
        except Exception: pass

# 🟢 START BACKGROUND TASK ON APP STARTUP 🟢
@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(oi_price_monitor())
