import os
import asyncio
import httpx
import time
import uuid
import traceback
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

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise HTTPException(status_code=401, detail="Kotak Session OFF! Please Login.")
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

# 🚀 NAYA MODEL: AUTO RECOVERY KE SATH 🚀
class CustomStrategyTwoConfig(BaseModel):
    index: str
    entry_time: str
    max_premium: float
    sl_pct: float
    active_days: List[int]
    lots: int = 1 
    auto_recovery: bool = False      # Auto Yes/No
    recovery_count: int = 0          # Kitni baar recovery karni hai

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
    if len(custom_strats) >= 5: return {"status": "error", "message": "Max 5 strategies allowed."}
    
    new_strat = strat.dict()
    new_strat["id"] = str(uuid.uuid4())[:8]
    new_strat["is_active"] = True
    new_strat["last_executed_date"] = ""
    if not new_strat.get("lots") or new_strat.get("lots") <= 0: new_strat["lots"] = 1
    
    await algo_col.update_one({"user_id": current_user["id"]}, {"$push": {"custom_strategies": new_strat}}, upsert=True)
    return {"status": "success", "message": "Strategy Added Successfully!"}

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

# 🚀 CORE STRADDLE EXECUTION ENGINE (Recovery Done support added) 🚀
async def core_algotwo_execution(user_id: str, mode: str, strategy: dict, recoveries_done: int = 0):
    now = datetime.now(IST)

    try:
        index = strategy.get("index", "NIFTY")
        target_premium = float(strategy.get("max_premium", 10.0))
        sl_pct = float(strategy.get("sl_pct", 200.0))
        lots = max(int(strategy.get("lots") or 1), 1)

        if index == "NIFTY": lot_size, gap = 65, 50
        elif index == "BANKNIFTY": lot_size, gap = 30, 100
        elif index == "SENSEX": lot_size, gap = 20, 100
        else: lot_size, gap = 65, 50

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

        atm = round(spot_ltp / gap) * gap
        cursor.sort(key=lambda x: abs(x["Strike"] - atm))
        
        tokens_req = [{"instrument_token": doc["Token"], "exchange_segment": conf["Exchange"], "type": doc["Type"], "sym": doc["Symbol"], "strike": doc["Strike"]} for doc in cursor]
        
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
                    ltp_map[tk] = float(item.get('ltp', 0))
                    
                for b in batch:
                    tk = b["instrument_token"]
                    if tk in ltp_map:
                        ltp = ltp_map[tk]
                        if 0 < ltp <= target_premium:
                            if b["type"] == "CE" and not best_ce: best_ce = {"sym": b["sym"], "tk": tk, "ltp": ltp, "type": "CE", "strike": b["strike"], "exch_seg": b["exchange_segment"]}
                            elif b["type"] == "PE" and not best_pe: best_pe = {"sym": b["sym"], "tk": tk, "ltp": ltp, "type": "PE", "strike": b["strike"], "exch_seg": b["exchange_segment"]}
                if best_ce and best_pe: break
            except: pass

        if not best_ce or not best_pe: return {"status": "error", "message": f"Could not find CE/PE <= ₹{target_premium}"}

        ce_entry = round_to_tick(best_ce["ltp"])
        pe_entry = round_to_tick(best_pe["ltp"])
        
        ce_sl_trigger = round_to_tick(ce_entry * (1 + sl_pct/100))
        pe_sl_trigger = round_to_tick(pe_entry * (1 + sl_pct/100))
        ce_sl_limit = round_to_tick(ce_sl_trigger + 10.0)
        pe_sl_limit = round_to_tick(pe_sl_trigger + 10.0)

        ce_sl_ord_no = ""
        pe_sl_ord_no = ""

        if mode == "REAL":
            try:
                uniq = str(int(time.time()))[-6:]
                
                def fire_order(order_name, **kwargs):
                    resp = client.place_order(**kwargs)
                    if not resp: raise Exception(f"{order_name} failed: Empty response from Kotak API.")
                    if isinstance(resp, dict):
                        if resp.get("stat") == "Not_Ok" or resp.get("stat") == "Rejected":
                            raise Exception(f"{order_name} Rejected: {resp.get('errMsg', resp.get('message', 'Broker reject'))}")
                        if "nOrdNo" not in resp and "nOrdNo" not in resp.get("data", {}):
                            if resp.get("stat") != "Ok": raise Exception(f"{order_name} Error: No Order ID generated.")
                    return resp

                ce_e_prc = round_to_tick(max(ce_entry - 3.0, 0.5))
                pe_e_prc = round_to_tick(max(pe_entry - 3.0, 0.5))
                
                # Sells
                await asyncio.gather(
                    asyncio.to_thread(fire_order, "CE Sell", exchange_segment=conf["Exchange"], product="NRML", price=str(ce_e_prc), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0"),
                    asyncio.to_thread(fire_order, "PE Sell", exchange_segment=conf["Exchange"], product="NRML", price=str(pe_e_prc), order_type="L", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="S", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0")
                )
                
                # SLs
                ce_sl_resp, pe_sl_resp = await asyncio.gather(
                    asyncio.to_thread(fire_order, "CE SL", exchange_segment=conf["Exchange"], product="NRML", price=str(ce_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_ce["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(ce_sl_trigger)),
                    asyncio.to_thread(fire_order, "PE SL", exchange_segment=conf["Exchange"], product="NRML", price=str(pe_sl_limit), order_type="SL", quantity=str(qty), validity="DAY", trading_symbol=best_pe["sym"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price=str(pe_sl_trigger))
                )
                
                if ce_sl_resp and isinstance(ce_sl_resp, dict): ce_sl_ord_no = str(ce_sl_resp.get("nOrdNo", ce_sl_resp.get("data", {}).get("nOrdNo", "")))
                if pe_sl_resp and isinstance(pe_sl_resp, dict): pe_sl_ord_no = str(pe_sl_resp.get("nOrdNo", pe_sl_resp.get("data", {}).get("nOrdNo", "")))

            except Exception as e: 
                return {"status": "error", "message": f"Kotak Error: {str(e)}"}
                
        db_col = get_collection("real_trades_two")
        trade_doc = {
            "user_id": user_id, 
            "strat_id": strategy.get("id"),
            "status": "OPEN", 
            "entry_time": now.strftime("%Y-%m-%d %H:%M:%S"), 
            "is_algo": True, 
            "algo_type": "ALGO_TWO",
            "max_recoveries": strategy.get("recovery_count", 0) if strategy.get("auto_recovery") else 0,
            "recoveries_done": recoveries_done,
            "legs": [
                {"type": "CE", "symbol": best_ce["sym"], "token": best_ce["tk"], "qty": int(qty), "entry_price": ce_entry, "sl": ce_sl_trigger, "sl_ord_no": ce_sl_ord_no, "exch_seg": conf["Exchange"]},
                {"type": "PE", "symbol": best_pe["sym"], "token": best_pe["tk"], "qty": int(qty), "entry_price": pe_entry, "sl": pe_sl_trigger, "sl_ord_no": pe_sl_ord_no, "exch_seg": conf["Exchange"]}
            ]
        }
        await db_col.insert_one(trade_doc)

        t_msg = f"🚀 <b>STRADDLE PLACED</b>\n\n🟢 <b>CE:</b> {best_ce['sym']} @ ₹{ce_entry}\n🔴 <b>PE:</b> {best_pe['sym']} @ ₹{pe_entry}\n🛡️ <b>SL:</b> {sl_pct}%\n🔄 <b>Recovery:</b> {recoveries_done}/{trade_doc['max_recoveries']}"
        asyncio.create_task(send_user_alert(user_id, t_msg))
        return {"status": "success", "message": "Executed successfully!"}
        
    except Exception as fatal_e:
        error_details = traceback.format_exc().splitlines()
        return {"status": "error", "message": f"Bug: {' | '.join(error_details[-2:])}"}


@router.post("/execute-now")
async def manual_trigger_algotwo(mode: str = "REAL", strat_id: str = None, current_user: dict = Depends(get_current_user)):
    algo_col = get_collection("algo_two_state")
    state = await algo_col.find_one({"user_id": current_user["id"]})
    strat_to_exec = None
    if strat_id and state:
        for s in state.get("custom_strategies", []):
            if s["id"] == strat_id: strat_to_exec = s; break
            
    if not strat_to_exec: raise HTTPException(status_code=400, detail="Strategy not found.")
        
    result = await core_algotwo_execution(current_user["id"], mode, strategy=strat_to_exec, recoveries_done=0)
    if result.get("status") == "error":
        raise HTTPException(status_code=400, detail=result.get("message", "Execution Failed"))
    return result


# 🚀 THE MAGIC: SL MONITOR & AUTO-RECOVERY SYSTEM 🚀
async def algotwo_recovery_monitor():
    last_pos_sync = 0
    pos_map = {}

    while True:
        try:
            db_col = get_collection("real_trades_two")
            user_col = get_collection("users")
            all_users = await user_col.find({}).to_list(length=None)
            
            for user in all_users:
                user_id = user["id"]
                try: client = get_kotak_client(user_id)
                except: continue
                
                active_trades = await db_col.find({"user_id": user_id, "status": "OPEN", "algo_type": "ALGO_TWO"}).to_list(length=None)
                if not active_trades: continue
                
                current_time = time.time()
                
                # Fetch positions exactly like algo.py
                if current_time - last_pos_sync > 5:
                    try:
                        pos_response = client.positions()
                        pos_data = pos_response["data"] if isinstance(pos_response, dict) and "data" in pos_response else (pos_response if isinstance(pos_response, list) else [])
                        pos_map = {str(p.get("tok", "")): int(float(p.get("flBuyQty", 0))) - int(float(p.get("flSellQty", 0))) for p in pos_data if (int(float(p.get("flBuyQty", 0))) - int(float(p.get("flSellQty", 0)))) != 0}
                        last_pos_sync = current_time
                    except: pass

                for t in active_trades:
                    # SAFETY GRACE PERIOD: 15 seconds after trade execution to allow Kotak to update positions
                    trade_time = datetime.strptime(t["entry_time"], "%Y-%m-%d %H:%M:%S")
                    if (datetime.now(IST).replace(tzinfo=None) - trade_time).total_seconds() < 15:
                        continue 

                    ce_leg = next((l for l in t["legs"] if l["type"] == "CE"), None)
                    pe_leg = next((l for l in t["legs"] if l["type"] == "PE"), None)
                    if not ce_leg or not pe_leg: continue

                    ce_pos = pos_map.get(str(ce_leg["token"]), 0)
                    pe_pos = pos_map.get(str(pe_leg["token"]), 0)

                    # If pos is 0, it means Kotak squared it off (SL Hit)
                    ce_hit = (ce_pos == 0)
                    pe_hit = (pe_pos == 0)

                    if ce_hit or pe_hit:
                        # Lock trade immediately to avoid double firing
                        lock = await db_col.update_one({"_id": t["_id"], "status": "OPEN"}, {"$set": {"status": "EXITING"}})
                        if lock.modified_count == 0: continue

                        asyncio.create_task(send_user_alert(user_id, f"⚠️ <b>SL HIT DETECTED!</b>\nCancelling pending orders & clearing open legs..."))

                        # 1. Cancel remaining SL orders from Kotak
                        try:
                            order_report = client.order_report()
                            orders = order_report if isinstance(order_report, list) else order_report.get("data", [])
                            for ord_data in orders:
                                ord_no = str(ord_data.get("nOrdNo", ""))
                                if ord_no in [ce_leg["sl_ord_no"], pe_leg["sl_ord_no"]]:
                                    status = str(ord_data.get("ordSt", "")).lower()
                                    if status in ["opn", "trg", "pending", "trigger pending", "open"]:
                                        try: client.cancel_order(nOrdNo=ord_no)
                                        except: pass
                        except: pass

                        # 2. Market exit for the remaining leg
                        if not ce_hit and ce_pos != 0:
                            try: client.place_order(exchange_segment=ce_leg["exch_seg"], product="NRML", price="0", order_type="MKT", quantity=str(abs(ce_pos)), validity="DAY", trading_symbol=ce_leg["symbol"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0")
                            except: pass
                        if not pe_hit and pe_pos != 0:
                            try: client.place_order(exchange_segment=pe_leg["exch_seg"], product="NRML", price="0", order_type="MKT", quantity=str(abs(pe_pos)), validity="DAY", trading_symbol=pe_leg["symbol"], transaction_type="B", amo="NO", disclosed_quantity="0", pf="N", trigger_price="0")
                            except: pass

                        await db_col.update_one({"_id": t["_id"]}, {"$set": {"status": "CLOSED"}})

                        # 3. AUTO RE-ENTRY LOGIC
                        recoveries_done = t.get("recoveries_done", 0)
                        max_rec = t.get("max_recoveries", 0)

                        if recoveries_done < max_rec:
                            strat_id = t.get("strat_id")
                            user_state = await get_collection("algo_two_state").find_one({"user_id": user_id})
                            strat_to_exec = next((s for s in user_state.get("custom_strategies", []) if s["id"] == strat_id), None)
                            
                            if strat_to_exec:
                                asyncio.create_task(send_user_alert(user_id, f"🔄 <b>AUTO RE-ENTRY INITIALIZED</b>\nAttempt: {recoveries_done + 1} of {max_rec}"))
                                await asyncio.sleep(2) # Give Kotak a 2-sec breather
                                asyncio.create_task(core_algotwo_execution(user_id, "REAL", strat_to_exec, recoveries_done + 1))
                            
        except Exception as e: pass
        await asyncio.sleep(2) # Loop every 2 seconds

async def automatic_algotwo_scheduler():
    while True:
        now = datetime.now(IST)
        current_time = now.strftime("%H:%M")
        current_date = now.strftime("%Y-%m-%d")
        algo_col = get_collection("algo_two_state")
        db_col = get_collection("real_trades_two")
        
        all_users = await algo_col.find({}).to_list(length=None)
        for user_state in all_users:
            if not user_state.get("is_active", False): continue
            
            # Daily Scheduled Entry
            custom_strats = user_state.get("custom_strategies", [])
            for i, strat in enumerate(custom_strats):
                if strat.get("is_active", True) and strat.get("last_executed_date") != current_date:
                    if strat.get("entry_time") == current_time:
                        lock = await algo_col.update_one({"_id": user_state["_id"], f"custom_strategies.{i}.last_executed_date": strat.get("last_executed_date")}, {"$set": {f"custom_strategies.{i}.last_executed_date": current_date}})
                        if lock.modified_count > 0:
                            asyncio.create_task(core_algotwo_execution(user_state["user_id"], "REAL", strat, 0))

            # 🚀 3:15 PM AUTO EXIT (Copied exactly from algo.py)
            if current_time == "15:15":
                user_id = user_state["user_id"]
                try:
                    client = get_kotak_client(user_id)
                    try: 
                        order_report = client.order_report()
                        orders = order_report if isinstance(order_report, list) else order_report.get("data", [])
                        for ord_data in orders:
                            trd_sym = str(ord_data.get("trdSym", "")).upper()
                            is_index_option = any(idx in trd_sym for idx in ["NIFTY", "SENSEX", "BANKNIFTY"]) and ("CE" in trd_sym or "PE" in trd_sym)
                            if not is_index_option: continue
                            status = str(ord_data.get("ordSt", "")).lower()
                            if status in ["opn", "trg", "pending", "trigger pending", "open", "put", "modified"]:
                                try: client.cancel_order(nOrdNo=str(ord_data.get("nOrdNo")))
                                except: pass
                    except: pass
                        
                    try:
                        pos_response = client.positions()
                        pos_data = pos_response["data"] if isinstance(pos_response, dict) and "data" in pos_response else (pos_response if isinstance(pos_response, list) else [])
                        exited_something = False
                        
                        for p in pos_data:
                            trd_sym = str(p.get("trdSym", "")).upper()
                            is_index_option = any(idx in trd_sym for idx in ["NIFTY", "SENSEX", "BANKNIFTY"]) and ("CE" in trd_sym or "PE" in trd_sym)
                            if not is_index_option: continue 
                                
                            buy_qty = int(float(p.get("flBuyQty", p.get("buyQty", 0))))
                            sell_qty = int(float(p.get("flSellQty", p.get("sellQty", 0))))
                            net_qty = buy_qty - sell_qty
                            
                            if net_qty != 0:
                                exit_trans = "S" if net_qty > 0 else "B" 
                                try:
                                    client.place_order(exchange_segment=p.get("exSeg", "nse_fo"), product="NRML", price="0", order_type="MKT", quantity=str(abs(net_qty)), validity="DAY", trading_symbol=p.get("trdSym"), transaction_type=exit_trans, amo="NO")
                                    exited_something = True
                                except: pass
                                    
                        if exited_something:
                            asyncio.create_task(send_user_alert(user_id, f"⏰ <b>AUTO EXIT @ 15:15</b>\n✅ Strategy squared off successfully."))
                    except: pass
                    await db_col.update_many({"user_id": user_id, "status": "OPEN"}, {"$set": {"status": "CLOSED"}})
                except: pass

        await asyncio.sleep(61 - datetime.now(IST).second)

@router.on_event("startup")
async def start_algotwo_tasks():
    asyncio.create_task(automatic_algotwo_scheduler())
    asyncio.create_task(algotwo_recovery_monitor())
