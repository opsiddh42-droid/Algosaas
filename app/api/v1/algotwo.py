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

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}" if BOT_TOKEN else None

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50, "SpotToken": "Nifty 50", "SpotExch": "nse_cm", "Coll": "nifty_strike_data"},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100, "SpotToken": "SENSEX", "SpotExch": "nse_cm", "Coll": "sensex_strike_data"},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100, "SpotToken": "Nifty Bank", "SpotExch": "nse_cm", "Coll": "banknifty_strike_data"}
}

# 🚀 IN-MEMORY WEBSOCKET CACHE (SUPER FAST) 🚀
LIVE_LTP_MEMORY = {}
SUBSCRIBED_TOKENS = set()

def kotak_on_message(message):
    try:
        if isinstance(message, list):
            for item in message:
                tk = str(item.get("tk", ""))
                ltp = float(item.get("ltp", item.get("lastPrice", 0)))
                if tk and ltp > 0: LIVE_LTP_MEMORY[tk] = ltp
        elif isinstance(message, dict):
            tk = str(message.get("tk", ""))
            ltp = float(message.get("ltp", message.get("lastPrice", 0)))
            if tk and ltp > 0: LIVE_LTP_MEMORY[tk] = ltp
    except: pass

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS: raise HTTPException(status_code=401, detail="Kotak Session OFF!")
    return KOTAK_SESSIONS[user_id]

def round_to_tick(price: float) -> float:
    return round(price * 20) / 20.0

async def send_user_alert(user_id: str, message: str):
    if not BOT_TOKEN or not TELEGRAM_API_URL: return
    try:
        user_col = get_collection("users")
        user = await user_col.find_one({"id": user_id})
        if user and user.get("telegram_chat_id"):
            async with httpx.AsyncClient() as client:
                await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": user["telegram_chat_id"], "text": message, "parse_mode": "HTML"}, timeout=10)
    except: pass

class CustomStrategyTwoConfig(BaseModel):
    index: str
    option_type: str  
    entry_time: str
    max_premium: float
    active_days: List[int]
    lots: int = 1 
    is_hedge: bool = False  
    hedge_pct: float = 10.0 
    target_type: str = "NONE" 
    target_val: float = 0.0

@router.get("/status")
async def get_algotwo_status(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    if not state:
        state = {"user_id": current_user["id"], "is_active": False, "custom_strategies": []}
        await algo_col.insert_one(state)

    now = datetime.now(IST)
    current_date_str = now.strftime("%Y-%m-%d")
    current_time = now.time()
    day = now.weekday()

    custom_strategies = state.get("custom_strategies", [])
    for strat in custom_strategies:
        try:
            strat_time = datetime.strptime(strat.get("entry_time", "10:00"), "%H:%M").time()
            if strat.get("last_executed_date") == current_date_str: strat["time_remaining"] = "Executed Today ✅"
            elif day not in strat.get("active_days", []): strat["time_remaining"] = "Not Active Today ⏸️"
            elif current_time < strat_time:
                mins = int((datetime.combine(now.date(), strat_time) - datetime.combine(now.date(), current_time)).total_seconds() // 60)
                strat["time_remaining"] = f"In {mins} mins ⏳"
            else: strat["time_remaining"] = "Time Passed ⌛"
        except: strat["time_remaining"] = "--"

    return {"status": "success", "is_active": state.get("is_active", False), "custom_strategies": custom_strategies}

@router.post("/toggle-master")
async def toggle_master_algotwo(current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    new_status = not state.get("is_active", False) if state else True
    await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {"is_active": new_status}}, upsert=True)
    return {"status": "success", "is_active": new_status}

@router.post("/add-strategy")
async def add_algotwo_strategy(strat: CustomStrategyTwoConfig, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    custom_strats = state.get("custom_strategies", []) if state else []
    if len(custom_strats) >= 5: return {"status": "error", "message": "Max 5 Trend strategies allowed."}
    
    new_strat = strat.dict()
    new_strat["id"] = str(uuid.uuid4())[:8]
    new_strat["is_active"] = True
    new_strat["last_executed_date"] = ""
    if not new_strat.get("lots") or new_strat.get("lots") <= 0: new_strat["lots"] = 1
    
    await algo_col.update_one({"user_id": current_user["id"]}, {"$push": {"custom_strategies": new_strat}}, upsert=True)
    return {"status": "success", "message": "Trend Strategy Added!"}

@router.delete("/delete-strategy/{strat_id}")
async def delete_algotwo_strategy(strat_id: str, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    await algo_col.update_one({"user_id": current_user["id"]}, {"$pull": {"custom_strategies": {"id": strat_id}}})
    return {"status": "success"}

@router.post("/toggle-strategy/{strat_id}")
async def toggle_algotwo_strategy(strat_id: str, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    for i, s in enumerate(state.get("custom_strategies", [])):
        if s["id"] == strat_id:
            new_val = not s.get("is_active", True)
            await algo_col.update_one({"user_id": current_user["id"]}, {"$set": {f"custom_strategies.{i}.is_active": new_val}})
            return {"status": "success"}
    return {"status": "error"}

async def core_algotwo_execution(user_id: str, mode: str, strategy: dict):
    now = datetime.now(IST)
    lock_col = get_collection("atomic_locks")
    lock_key = f"algotwo_{user_id}_{strategy.get('id', 'manual')}"
    
    try:
        await lock_col.delete_one({"_id": lock_key, "timestamp": {"$lt": now - timedelta(seconds=15)}})
        await lock_col.insert_one({"_id": lock_key, "timestamp": now})
    except Exception:
        return {"status": "error", "message": "Execution already processing."}

    try:
        index = strategy.get("index", "NIFTY")
        opt_type = strategy.get("option_type", "CE")
        target_premium = float(strategy.get("max_premium", 10.0))
        lots = max(int(strategy.get("lots") or 1), 1)
        
        raw_hedge = strategy.get("is_hedge", False)
        is_hedge = str(raw_hedge).strip().lower() in ['true', '1', 'yes'] if isinstance(raw_hedge, str) else bool(raw_hedge)
        hedge_pct = float(strategy.get("hedge_pct", 10.0))
        
        tgt_type = strategy.get("target_type", "NONE")
        tgt_val = float(strategy.get("target_val", 0.0))

        if index == "NIFTY": lot_size = 65
        elif index == "BANKNIFTY": lot_size = 30
        elif index == "SENSEX": lot_size = 20
        else: lot_size = 65

        qty = str(lots * lot_size)

        try: client = get_kotak_client(user_id)
        except: return {"status": "error", "message": "Kotak Session OFF!"}

        conf = INDICES_CONFIG.get(index)
        try:
            q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
            spot_ltp = float(q[0].get('ltp', 0) if isinstance(q, list) else q.get('data', [{}])[0].get('ltp', 0))
        except: spot_ltp = 0
            
        coll = get_collection(conf["Coll"])
        cursor = await coll.find().to_list(length=None)
        if not cursor or spot_ltp == 0: return {"status": "error", "message": f"No Sync data or Spot Price."}

        # 🚀 ALGO.PY EXACT SEARCH LOGIC 🚀
        atm = round(spot_ltp / conf["Gap"]) * conf["Gap"]
        cursor.sort(key=lambda x: abs(x["Strike"] - atm))
        
        tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": conf["Exchange"], "type": doc["Type"], "sym": doc["Symbol"], "strike": doc["Strike"]} for doc in cursor]
        
        best_leg = None
        ltp_map = {}
        
        for i in range(0, len(tokens_req), 50):
            batch = tokens_req[i:i+50]
            try:
                req_batch = [{"instrument_token": b["instrument_token"], "exchange_segment": b["exchange_segment"]} for b in batch]
                raw = client.quotes(instrument_tokens=req_batch, quote_type="all")
                raw_data = raw if isinstance(raw, list) else raw.get('data', [])
                
                for item in raw_data:
                    tk = str(item.get('exchange_token', item.get('tk')))
                    ltp_map[tk] = float(item.get('ltp', 0))
                    
                for b in batch:
                    tk = b["instrument_token"]
                    if tk in ltp_map and b["type"] == opt_type:
                        ltp = ltp_map[tk]
                        if 0 < ltp <= target_premium:
                            best_leg = {"sym": b["sym"], "tk": tk, "ltp": ltp, "type": opt_type, "strike": b["strike"]}
                            break
                if best_leg: break
            except: pass

        if not best_leg: return {"status": "error", "message": f"Could not find {opt_type} <= ₹{target_premium}"}

        hedge_leg = None
        if is_hedge and hedge_pct > 0:
            target_hedge_prc = best_leg["ltp"] * (hedge_pct / 100.0)
            for b in tokens_req:
                if (opt_type == "CE" and b["strike"] > best_leg["strike"]) or (opt_type == "PE" and b["strike"] < best_leg["strike"]):
                    tk = b["instrument_token"]
                    if tk not in ltp_map:
                        try:
                            rq = client.quotes(instrument_tokens=[{"instrument_token": tk, "exchange_segment": b["exchange_segment"]}], quote_type="ltp")
                            ltp_map[tk] = float((rq if isinstance(rq, list) else rq.get('data', [{}]))[0].get('ltp', 0))
                        except: pass
                    
                    if tk in ltp_map:
                        ltp = ltp_map[tk]
                        if 0 < ltp <= target_hedge_prc:
                            hedge_leg = {"sym": b["sym"], "tk": tk, "ltp": ltp, "strike": b["strike"]}
                            break 

        # 🚀 EXACT ALGO.PY ORDER PARAMS & BUFFER LOGIC 🚀
        entry_ltp = round_to_tick(best_leg["ltp"])
        sl_trigger = round_to_tick(entry_ltp * 2.0)
        sl_limit = round_to_tick(sl_trigger + 10.0)
        sl_ord_no = ""

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

                if hedge_leg:
                    h_prc = round_to_tick(hedge_leg["ltp"] + 1.0) 
                    try:
                        await asyncio.to_thread(fire_order, "Hedge Buy", exchange_segment=conf["Exchange"], product="NRML", price=str(h_prc), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=hedge_leg["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"h_{uniq}")
                        await asyncio.sleep(0.5) 
                    except Exception as he: pass 

                # EXACT MATCH TO ALGO.PY: max(entry_ltp - 3.0, 0.5)
                e_prc = round_to_tick(max(entry_ltp - 3.0, 0.5)) 
                await asyncio.to_thread(fire_order, "Main Sell", exchange_segment=conf["Exchange"], product="NRML", price=str(e_prc), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_leg["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0", tag=f"e_{uniq}")
                
                sl_resp = await asyncio.to_thread(fire_order, "SL Buy", exchange_segment=conf["Exchange"], product="NRML", price=str(sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_leg["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(sl_trigger), tag=f"s_{uniq}")
                
                if sl_resp and isinstance(sl_resp, dict):
                    sl_ord_no = sl_resp.get("nOrdNo", sl_resp.get("data", {}).get("nOrdNo", ""))

            except Exception as e: return {"status": "error", "message": f"{str(e)}"}
                
        db_col = get_collection("real_trades")
        legs_arr = [{"symbol": best_leg["sym"], "token": best_leg["tk"], "transaction": "S", "qty": int(qty), "entry_price": entry_ltp, "ltp": entry_ltp, "exch_seg": conf["Exchange"]}]
        if hedge_leg: legs_arr.append({"symbol": hedge_leg["sym"], "token": hedge_leg["tk"], "transaction": "B", "qty": int(qty), "entry_price": hedge_leg["ltp"], "ltp": hedge_leg["ltp"], "exch_seg": conf["Exchange"]})

        await db_col.insert_one({
            "user_id": user_id, "status": "OPEN", "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), 
            "is_algo": True, "algo_type": "ALGO_TWO", 
            "sl_ord_no": sl_ord_no, "target_type": tgt_type, "target_val": tgt_val, "legs": legs_arr
        })

        t_msg = f"🧭 <b>{opt_type} SOLD (Trend)</b>\n\n🎯 <b>Sold:</b> {best_leg['sym']} @ ₹{entry_ltp}\n🛡️ <b>SL:</b> ₹{sl_trigger} (Limit: ₹{sl_limit})\n🎯 <b>Target:</b> {tgt_val} {tgt_type}"
        if hedge_leg: t_msg += f"\n🛡️ <b>Hedge:</b> {hedge_leg['sym']} @ ₹{hedge_leg['ltp']}"
        asyncio.create_task(send_user_alert(user_id, t_msg))
        return {"status": "success", "message": f"Executed: {best_leg['sym']}"}
        
    finally:
        try: await lock_col.delete_one({"_id": lock_key})
        except: pass

@router.post("/execute-now")
async def manual_trigger_algotwo(mode: str = "REAL", strat_id: str = None, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    strat_to_exec = None
    if strat_id and state:
        for s in state.get("custom_strategies", []):
            if s["id"] == strat_id: strat_to_exec = s; break
            
    if not strat_to_exec: 
        raise HTTPException(status_code=400, detail="Strategy not found.")
        
    result = await core_algotwo_execution(current_user["id"], mode, strategy=strat_to_exec)
    
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Execution Failed"))
        
    return result

async def algotwo_ws_trade_monitor():
    last_pos_sync = 0
    pos_map = {}

    while True:
        try:
            db_col = get_collection("real_trades")
            user_col = get_collection("users")
            all_users = await user_col.find({}).to_list(length=None)
            
            for user in all_users:
                user_id = user["id"]
                try: client = get_kotak_client(user_id)
                except: continue
                
                if not hasattr(client, "ws_is_setup"):
                    client.on_message = kotak_on_message
                    client.ws_is_setup = True
                
                active_trades = await db_col.find({"user_id": user_id, "status": "OPEN", "algo_type": "ALGO_TWO"}).to_list(length=None)
                if not active_trades: continue
                
                current_time = time.time()
                if current_time - last_pos_sync > 5:
                    try:
                        pos_response = client.positions()
                        pos_data = pos_response["data"] if isinstance(pos_response, dict) and "data" in pos_response else (pos_response if isinstance(pos_response, list) else [])
                        pos_map = {str(p.get("tok", "")): int(float(p.get("flBuyQty", 0))) - int(float(p.get("flSellQty", 0))) for p in pos_data if (int(float(p.get("flBuyQty", 0))) - int(float(p.get("flSellQty", 0)))) != 0}
                        last_pos_sync = current_time
                    except: pass

                tokens_to_subscribe = []
                for t in active_trades:
                    leg = t["legs"][0]
                    tok = str(leg["token"])
                    
                    if tok not in pos_map and (current_time - last_pos_sync < 6):
                        await db_col.update_one({"_id": t["_id"]}, {"$set": {"status": "CLOSED"}})
                        continue
                        
                    if t.get("target_type", "NONE") != "NONE":
                        if tok not in SUBSCRIBED_TOKENS:
                            tokens_to_subscribe.append({"instrument_token": tok, "exchange_segment": leg["exch_seg"]})
                            SUBSCRIBED_TOKENS.add(tok)
                            
                if tokens_to_subscribe:
                    try: client.subscribe(instrument_tokens=tokens_to_subscribe)
                    except: pass

                for t in active_trades:
                    if t["status"] == "OPEN" and t.get("target_type") != "NONE":
                        leg = t["legs"][0]
                        tok = str(leg["token"])
                        ltp = LIVE_LTP_MEMORY.get(tok, 0) 
                        
                        if ltp > 0:
                            entry = float(leg["entry_price"])
                            qty = int(leg["qty"])
                            tgt_type = t["target_type"]
                            tgt_val = float(t["target_val"])
                            trigger_exit = False
                            
                            if tgt_type == "POINTS":
                                if (entry - ltp) >= tgt_val: trigger_exit = True
                            elif tgt_type == "RUPEE":
                                if (entry - ltp) * qty >= tgt_val: trigger_exit = True
                                
                            if trigger_exit:
                                sl_ord = t.get("sl_ord_no")
                                if sl_ord:
                                    try: client.cancel_order(nOrdNo=sl_ord)
                                    except: pass
                                
                                safe_exit_price = round_to_tick(ltp + 5.0)
                                
                                try:
                                    # Exact algo.py MKT imitation format
                                    client.place_order(exchange_segment=leg["exch_seg"], product="NRML", price=str(safe_exit_price), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=leg["symbol"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0")
                                    await db_col.update_one({"_id": t["_id"]}, {"$set": {"status": "CLOSED"}})
                                    asyncio.create_task(send_user_alert(user_id, f"🎯 <b>TARGET ACHIEVED!</b>\n\n✅ Booked profit for {leg['symbol']} at ₹{ltp}"))
                                except: pass
        except: pass
        await asyncio.sleep(0.2) 

async def automatic_algotwo_scheduler():
    while True:
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")
        algo_col = get_collection("algo_two_state")
        
        all_users = await algo_col.find({}).to_list(length=None)
        for user_state in all_users:
            if not user_state.get("is_active", False): continue
            
            custom_strats = user_state.get("custom_strategies", [])
            for i, strat in enumerate(custom_strats):
                if strat.get("is_active", True) and strat.get("last_executed_date") != current_date:
                    if strat.get("entry_time") == current_time:
                        lock = await algo_col.update_one({"_id": user_state["_id"], f"custom_strategies.{i}.last_executed_date": strat.get("last_executed_date")}, {"$set": {f"custom_strategies.{i}.last_executed_date": current_date}})
                        if lock.modified_count > 0:
                            asyncio.create_task(core_algotwo_execution(user_state["user_id"], "REAL", strategy=strat))
                            
        await asyncio.sleep(61 - datetime.now(IST).second)

@router.on_event("startup")
async def start_algotwo_tasks():
    asyncio.create_task(automatic_algotwo_scheduler())
    asyncio.create_task(algotwo_ws_trade_monitor())
