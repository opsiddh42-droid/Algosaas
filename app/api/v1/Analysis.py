import os
import asyncio
import httpx
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
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
    if not BOT_TOKEN or not TELEGRAM_API_URL: 
        print(f"⚠️ Bot token missing. Cannot send telegram message to {user_id}")
        return
    try:
        user_col = get_collection("users")
        user = await user_col.find_one({"id": user_id})
        if user and user.get("telegram_chat_id"):
            chat_id = user["telegram_chat_id"]
            async with httpx.AsyncClient() as client:
                resp = await client.post(f"{TELEGRAM_API_URL}/sendMessage", json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"}, timeout=10)
                if resp.status_code != 200:
                    print(f"⚠️ Telegram API Error: {resp.text}")
                else:
                    print(f"✅ Telegram message successfully sent to {user_id}")
        else:
            print(f"❌ Cannot send message: telegram_chat_id not found in database for user {user_id}")
    except Exception as e:
        print(f"⚠️ Error in send_user_alert: {e}")

class AlertConfig(BaseModel):
    is_active: bool
    index: str
    strikes: int

@router.get("/market-intelligence")
async def get_market_intelligence(symbol: str = "NIFTY", strikes_count: int = 10, current_user: dict = Depends(get_current_user)):
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
            if stk not in strike_map: strike_map[stk] = {"strike": stk, "ce_token": None, "pe_token": None}
            if doc["Type"] == "CE": strike_map[stk]["ce_token"] = doc["Token"]
            else: strike_map[stk]["pe_token"] = doc["Token"]

        all_strikes = sorted(strike_map.keys())
        
        if strikes_count != 100: 
            try: atm_index = all_strikes.index(atm)
            except ValueError: atm_index = min(range(len(all_strikes)), key=lambda i: abs(all_strikes[i] - atm))
            half = strikes_count // 2
            start_idx = max(0, atm_index - half)
            end_idx = min(len(all_strikes), atm_index + half + (1 if strikes_count%2!=0 else 0))
            active_strikes = all_strikes[start_idx:end_idx]
        else:
            active_strikes = all_strikes

        req_tokens = []
        for stk in active_strikes:
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

                    for stk in active_strikes:
                        if strike_map[stk].get("ce_token") == tk:
                            strike_map[stk].update({"ce_oi": oi, "ce_chg": oi_chg})
                            ce_tot_oi += oi; ce_tot_chg += oi_chg
                        elif strike_map[stk].get("pe_token") == tk:
                            strike_map[stk].update({"pe_oi": oi, "pe_chg": oi_chg})
                            pe_tot_oi += oi; pe_tot_chg += oi_chg
            except: pass

        analytics_data = [strike_map[s] for s in active_strikes]
        max_pain = calculate_max_pain(analytics_data)
        pcr = round(pe_tot_oi / ce_tot_oi, 2) if ce_tot_oi > 0 else 0
        
        trend = "BULLISH 🚀" if pcr >= 1.1 else "BEARISH 🩸" if pcr <= 0.9 else "SIDEWAYS ⚖️"
        diff_oi = pe_tot_oi - ce_tot_oi
        diff_text = f"PE Writers Dominating by {format_oi(abs(diff_oi))}" if diff_oi > 0 else f"CE Writers Dominating by {format_oi(abs(diff_oi))}"

        return {
            "status": "success",
            "spot": spot_ltp,
            "maxPain": max_pain,
            "pcr": pcr,
            "trend": trend,
            "ceTotalOi": format_oi(ce_tot_oi),
            "peTotalOi": format_oi(pe_tot_oi),
            "ceOiChange": format_oi(ce_tot_chg),
            "peOiChange": format_oi(pe_tot_chg),
            "difference": diff_text,
            "lastUpdated": datetime.now(IST).strftime("%H:%M:%S")
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/toggle-analysis-alert")
async def toggle_analysis_alert(config: AlertConfig, current_user: dict = Depends(get_current_user)):
    user_col = get_collection("users")
    await user_col.update_one(
        {"id": current_user["id"]}, 
        {"$set": {"analysis_alerts": {"is_active": config.is_active, "index": config.index, "strikes": config.strikes}}},
        upsert=True
    )
    
    # 🟢 INSTANT MESSAGE JAISE HI ON HOGA 🟢
    if config.is_active:
        msg = f"✅ <b>Alerts Activated for {config.index}!</b>\nAapko har 15 minute mein ({config.strikes} Strikes) ka data milta rahega."
        asyncio.create_task(send_user_alert(current_user["id"], msg))

    return {"status": "success", "message": "Alert updated."}

async def telegram_intelligence_scheduler():
    while True:
        now_time = datetime.now(IST).time()
        # Check if market is open
        if datetime.strptime("09:15", "%H:%M").time() <= now_time <= datetime.strptime("15:30", "%H:%M").time():
            try:
                user_col = get_collection("users")
                active_users = await user_col.find({"analysis_alerts.is_active": True}).to_list(length=None)
                
                for user in active_users:
                    prefs = user.get("analysis_alerts", {})
                    index = prefs.get("index", "NIFTY")
                    strikes = int(prefs.get("strikes", 10))
                    
                    try:
                        # Fetch the data
                        data = await get_market_intelligence(symbol=index, strikes_count=strikes, current_user={"id": user["id"]})
                        
                        if data.get("status") == "success":
                            t_msg = (
                                f"📊 <b>{index} INTRADAY PREDICTION</b>\n\n"
                                f"🎯 <b>Spot:</b> {data['spot']}\n"
                                f"⚖️ <b>Max Pain:</b> {data['maxPain']}\n"
                                f"📈 <b>PCR:</b> {data['pcr']} ({data['trend']})\n\n"
                                f"🔍 <b>LIVE DATA ({strikes} Strikes)</b>\n"
                                f"🔴 <b>Total CE OI:</b> {data['ceTotalOi']} (Chg: {data['ceOiChange']})\n"
                                f"🟢 <b>Total PE OI:</b> {data['peTotalOi']} (Chg: {data['peOiChange']})\n\n"
                                f"💥 <b>Result:</b> {data['difference']}"
                            )
                            await send_user_alert(user["id"], t_msg)
                    except Exception as inner_e:
                        print(f"⚠️ Scheduler Data Error for {user['id']}: {inner_e}")
                        
            except Exception as e:
                print(f"⚠️ Scheduler DB Error: {e}")
        
        # Wait for 15 minutes AFTER checking/sending
        await asyncio.sleep(900)

@router.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(telegram_intelligence_scheduler())
