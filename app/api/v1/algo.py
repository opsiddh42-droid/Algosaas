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
from bson import ObjectId

load_dotenv()

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "") 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

class AlgoConfig(BaseModel):
    use_default: bool
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float
    active_days: List[int]

def round_to_tick(price: float) -> float:
    return round(price * 20) / 20.0

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
    
    default_config = {"use_default": True, "index": "NIFTY", "entry_time": "10:00", "max_premium": 6.0, "sl_pct": 200.0, "active_days": []}
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
        if day not in [0, 1, 4]: return {"status": "error", "message": "No default strategy planned for today."}
        index, target_premium, sl_pct = ("SENSEX", 12.0, 200.0) if day == 1 else ("NIFTY", 6.0, 200.0)
    else:
        if day not in config.get("active_days", []): return {"status": "error", "message": "Custom strategy is not configured to run today."}
        index = config.get("index", "NIFTY")
        target_premium, sl_pct = float(config.get("max_premium", 6.0)), float(config.get("sl_pct", 200.0))

    qty = "20" if index in ["SENSEX", "BANKNIFTY"] else "65"
    coll_name, exch_seg = f"{index.lower()}_strike_data", "bse_fo" if index == "SENSEX" else "nse_fo"

    try: client = get_kotak_client(user_id)
    except HTTPException: return {"status": "error", "message": "Kotak Session OFF! Please Login."}

    coll = get_collection(coll_name)
    cursor = await coll.find().to_list(length=None)
    if not cursor: return {"status": "error", "message": f"No {index} data in DB. Update Weekly Expiry!"}

    tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": exch_seg} for doc in cursor]
    ce_list, pe_list = [] , []
    
    for i in range(0, len(tokens_req), 50):
        try:
            raw = client.quotes(instrument_tokens=tokens_req[i:i+50], quote_type="all")
            raw_data = raw if isinstance(raw, list) else raw.get('data', [])
            for item in raw_data:
                tk, ltp, oi = str(item.get('exchange_token', item.get('tk'))), float(item.get('ltp', 0)), float(item.get('open_int', item.get('oi', 0)))
                doc = next((d for d in cursor if d["Token"] == tk), None)
                if doc and ltp > 0:
                    if doc["Type"] == "CE": ce_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp, "oi": oi})
                    else: pe_list.append({"sym": doc["Symbol"], "tk": tk, "ltp": ltp, "oi": oi})
        except: pass

    ce_list.sort(key=lambda x: x["ltp"], reverse=True)
    pe_list.sort(key=lambda x: x["ltp"], reverse=True)
    best_ce = next((x for x in ce_list if x["ltp"] <= target_premium), None)
    best_pe = next((x for x in pe_list if x["ltp"] <= target_premium), None)

    if not best_ce or not best_pe: return {"status": "error", "message": f"Could not find {index} CE/PE below ₹{target_premium}"}

    ce_ltp, pe_ltp = round_to_tick(best_ce["ltp"]), round_to_tick(best_pe["ltp"])
    ce_sl_trigger = round_to_tick(ce_ltp * (1 + sl_pct/100))
    pe_sl_trigger = round_to_tick(pe_ltp * (1 + sl_pct/100))
    
    ce_sl_limit, pe_sl_limit = round_to_tick(ce_sl_trigger + 10.0), round_to_tick(pe_sl_trigger + 10.0)

    if mode == "REAL":
        try:
            uniq = str(int(time.time()))[-6:]
            def fire_order(tag, **kwargs):
                resp = client.place_order(**kwargs)
                if isinstance(resp, dict) and resp.get("stat") != "Ok": raise Exception(str(resp))
                return resp

            fire_order("CE Ent", exchange_segment=exch_seg, product="NRML", price=str(ce_ltp), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"ce_e_{uniq}")
            fire_order("PE Ent", exchange_segment=exch_seg, product="NRML", price=str(pe_ltp), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"pe_e_{uniq}")
            fire_order("CE SL", exchange_segment=exch_seg, product="NRML", price=str(ce_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(ce_sl_trigger), tag=f"ce_s_{uniq}")
            fire_order("PE SL", exchange_segment=exch_seg, product="NRML", price=str(pe_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(pe_sl_trigger), tag=f"pe_s_{uniq}")
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

async def automatic_algo_scheduler():
    while True:
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")

        algo_col = get_collection("algo_state")
        db_col = get_collection("real_trades")
        
        # 🟢 1. CHECK FOR ENTRY
        active_users = await algo_col.find({"is_active": True, "last_executed_date": {"$ne": current_date}}).to_list(length=None)
        for user_state in active_users:
            if user_state.get("config", {}).get("entry_time", "10:00") == current_time:
                try: await core_algo_execution(user_state["user_id"], "REAL")
                except: pass

        # 🟢 2. CHECK FOR 3:15 PM AUTO EXIT
        if current_time == "15:15":
            open_trades = await db_col.find({"status": "OPEN"}).to_list(length=None)
            for trade in open_trades:
                user_id = trade["user_id"]
                try:
                    client = get_kotak_client(user_id)
                    updated_legs = []
                    for leg in trade["legs"]:
                        if leg["status"] == "OPEN":
                            # Ulta order (Buy if S, Sell if B)
                            exit_trans = "B" if leg["transaction"] == "S" else "S"
                            try:
                                client.place_order(exchange_segment=leg.get("exch_seg", "nse_fo"), product="NRML", price="0", order_type="MKT", quantity=str(leg["qty"]), validity="DAY", trading_symbol=leg["symbol"], transaction_type=exit_trans, amo="NO")
                                leg["status"] = "CLOSED"
                            except Exception as e:
                                print(f"Auto Exit Failed {leg['symbol']}: {e}")
                        updated_legs.append(leg)
                    
                    await db_col.update_one({"_id": trade["_id"]}, {"$set": {"status": "CLOSED", "legs": updated_legs}})
                    asyncio.create_task(send_user_alert(user_id, f"⏰ <b>AUTO EXIT @ 15:15</b>\n\nAll open Algo positions for {current_date} squared off automatically."))
                except Exception as e: pass

        sleep_sec = 60 - datetime.now(IST).second
        await asyncio.sleep(sleep_sec)

@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(automatic_algo_scheduler())
