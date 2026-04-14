import os
import asyncio
import httpx
from fastapi import APIRouter, HTTPException, Depends
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
    # Keep only the last 2 hours to avoid memory leaks
    cutoff = now - timedelta(hours=2)
    GLOBAL_SNAPSHOTS[index] = [s for s in GLOBAL_SNAPSHOTS[index] if s["time"] >= cutoff]

def get_1h_change(index: str, current_ce_tot: float, current_pe_tot: float):
    if not GLOBAL_SNAPSHOTS[index]: return 0, 0, 0
    target_time = datetime.now(IST) - timedelta(hours=1)
    # Find the snapshot closest to 1 hour ago
    closest = min(GLOBAL_SNAPSHOTS[index], key=lambda x: abs((x["time"] - target_time).total_seconds()))
    
    # If the closest snapshot is too recent (e.g., bot just started), use it, but indicate the actual minutes
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
    if abs_val >= 10000000:
        res = f"{abs_val / 10000000:.2f} Cr"
    elif abs_val >= 100000:
        res = f"{abs_val / 100000:.2f} L"
    else:
        res = str(int(abs_val))
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
            if current_strike > stk:
                total_loss += (current_strike - stk) * opt.get("ce_oi", 0)
            elif current_strike < stk:
                total_loss += (stk - current_strike) * opt.get("pe_oi", 0)
        if total_loss < min_loss:
            min_loss = total_loss
            max_pain_strike = current_strike
    return max_pain_strike

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
class TotalAlertConfig(BaseModel):
    is_active: bool
    index: str

class CustomStrikeConfig(BaseModel):
    is_active: bool
    index: str
    strikes: List[int] = Field(..., max_items=5)
    frequency_minutes: int # Allowed: 3, 5, 15, 30, 60

# --- API ENDPOINTS ---
@router.post("/toggle-total-alert")
async def toggle_total_alert(config: TotalAlertConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    await user_col.update_one(
        {"id": current_user["id"]}, 
        {"$set": {"total_alerts": {
            "is_active": config.is_active, 
            "index": config.index, 
            "last_sent_time": None,
            "min_frequency": 15 # 🚀 STRICT 15 MINS FOR TOTAL
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
    
    # 🚀 ENFORCE MINIMUM 3 MINUTES LIMIT
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

        # 🟢 Support & Resistance Logic 🟢
        valid_strikes = [s for s in strike_map.values() if s["ce_oi"] > 0 or s["pe_oi"] > 0]
        
        above_atm = [s for s in valid_strikes if s["strike"] > atm]
        resistances = sorted(above_atm, key=lambda x: x["ce_oi"], reverse=True)[:3]
        resistances = sorted(resistances, key=lambda x: x["strike"]) # R1, R2, R3 (Ascending)
        
        below_atm = [s for s in valid_strikes if s["strike"] < atm]
        supports = sorted(below_atm, key=lambda x: x["pe_oi"], reverse=True)[:3]
        supports = sorted(supports, key=lambda x: x["strike"], reverse=True) # S1, S2, S3 (Descending)

        # Record snapshot & get 1-Hour change
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
            "raw_strikes": strike_map, # Hidden backend data for custom alerts
            "lastUpdated": datetime.now(IST).strftime("%H:%M:%S")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# --- SCHEDULER ---
async def telegram_intelligence_scheduler():
    while True:
        now_dt = datetime.now(IST)
        now_time = now_dt.time()
        
        # Run only during market hours
        if datetime.strptime("09:15", "%H:%M").time() <= now_time <= datetime.strptime("15:30", "%H:%M").time():
            try:
                user_col = get_collection("users")
                
                # Fetch all users that have either total or custom alerts active
                active_users = await user_col.find({
                    "$or": [
                        {"total_alerts.is_active": True},
                        {"custom_alerts.is_active": True}
                    ]
                }).to_list(length=None)
                
                for user in active_users:
                    # 1️⃣ TOTAL ANALYSIS ALERT (15 Mins Fixed)
                    total_conf = user.get("total_alerts", {})
                    if total_conf.get("is_active"):
                        last_total = total_conf.get("last_sent_time")
                        should_send = False
                        
                        if not last_total:
                            should_send = True
                        else:
                            last_total_dt = datetime.fromisoformat(last_total)
                            if (now_dt - last_total_dt).total_seconds() >= 15 * 60: # 🚀 STRICT 15 MINS
                                should_send = True
                                
                        if should_send:
                            idx = total_conf.get("index", "NIFTY")
                            try:
                                data = await get_market_intelligence(symbol=idx, current_user={"id": user["id"]})
                                if data.get("status") == "success":
                                    s = data["supports"]
                                    r = data["resistances"]
                                    
                                    t_msg = (
                                        f"📊 <b>{idx} MASTER PREDICTION</b>\n\n"
                                        f"🎯 <b>Spot:</b> {data['spot']}\n"
                                        f"⚖️ <b>Max Pain:</b> {data['maxPain']}\n"
                                        f"📈 <b>PCR:</b> {data['pcr']} ({data['trend']})\n\n"
                                        f"🛡️ <b>KEY LEVELS</b>\n"
                                        f"🔺 <b>R3:</b> {r[2] if len(r)>2 else '-'} | <b>R2:</b> {r[1] if len(r)>1 else '-'} | <b>R1:</b> {r[0] if len(r)>0 else '-'}\n"
                                        f"🔻 <b>S1:</b> {s[0] if len(s)>0 else '-'} | <b>S2:</b> {s[1] if len(s)>1 else '-'} | <b>S3:</b> {s[2] if len(s)>2 else '-'}\n\n"
                                        f"📅 <b>DAY OPEN INTEREST (All Strikes)</b>\n"
                                        f"🔴 <b>CE OI:</b> {data['ceTotalOi']} (Added: {data['ceDayChange']})\n"
                                        f"🟢 <b>PE OI:</b> {data['peTotalOi']} (Added: {data['peDayChange']})\n"
                                        f"⚔️ <b>Status:</b> {data['difference']}\n\n"
                                        f"⏳ <b>LAST {data['historyMins']} MINS ACTIVITY</b>\n"
                                        f"🔴 <b>CE Change:</b> {data['ce1HourChange']}\n"
                                        f"🟢 <b>PE Change:</b> {data['pe1HourChange']}"
                                    )
                                    await send_user_alert(user["id"], t_msg)
                                    await user_col.update_one({"id": user["id"]}, {"$set": {"total_alerts.last_sent_time": now_dt.isoformat()}})
                            except Exception as e: print(f"Total Alert Error: {e}")

                    # 2️⃣ CUSTOM STRIKE ALERT (User Configured Frequency, Min 3 Mins)
                    cust_conf = user.get("custom_alerts", {})
                    if cust_conf.get("is_active"):
                        last_cust = cust_conf.get("last_sent_time")
                        freq = max(3, cust_conf.get("frequency_minutes", 3)) # 🚀 STRICT MINIMUM 3 MINS
                        should_send = False
                        
                        if not last_cust:
                            should_send = True
                        else:
                            last_cust_dt = datetime.fromisoformat(last_cust)
                            if (now_dt - last_cust_dt).total_seconds() >= freq * 60:
                                should_send = True
                                
                        if should_send:
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
                                            c_msg += f"⚡ <b>Strike: {stk}</b>\n"
                                            c_msg += f"🔴 CE OI: {format_oi(s_data['ce_oi'])} (Chg: {format_oi(s_data['ce_chg'])})\n"
                                            c_msg += f"🟢 PE OI: {format_oi(s_data['pe_oi'])} (Chg: {format_oi(s_data['pe_chg'])})\n\n"
                                            
                                    await send_user_alert(user["id"], c_msg.strip())
                                    await user_col.update_one({"id": user["id"]}, {"$set": {"custom_alerts.last_sent_time": now_dt.isoformat()}})
                            except Exception as e: print(f"Custom Alert Error: {e}")
                            
            except Exception as e:
                print(f"⚠️ Scheduler Core Error: {e}")
        
        # Check every 1 minute
        await asyncio.sleep(60)

@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(telegram_intelligence_scheduler())
