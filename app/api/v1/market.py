import os
import requests
import pandas as pd
import re
import calendar
import asyncio
from fastapi import APIRouter, HTTPException, Depends
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
from datetime import datetime, timedelta, timezone

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50, "SpotToken": "Nifty 50", "SpotExch": "nse_cm", "Coll": "nifty_strike_data"},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100, "SpotToken": "SENSEX", "SpotExch": "nse_cm", "Coll": "sensex_strike_data"},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100, "SpotToken": "Nifty Bank", "SpotExch": "nse_cm", "Coll": "banknifty_strike_data"}
}

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise HTTPException(status_code=401, detail="Kotak Session OFF! Please Login.")
    return KOTAK_SESSIONS[user_id]

# =========================================
# ⚙️ CORE SYNC ENGINE (Used by Manual & Auto)
# =========================================
async def perform_master_sync(user_id: str):
    client = get_kotak_client(user_id)
    total_loaded = 0

    for idx_name, conf in INDICES_CONFIG.items():
        spot_ltp = 0
        try:
            q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
            item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
            spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))
        except: pass
        
        if spot_ltp == 0 and idx_name == "SENSEX": 
            q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
            item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
            spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))

        gap = conf["Gap"]
        atm = (int(spot_ltp) - (int(spot_ltp) % gap)) if (int(spot_ltp) % gap) < (gap/2) else (int(spot_ltp) + (gap - (int(spot_ltp) % gap)))

        seg = conf["Exchange"]
        master_file = f"{seg}_master.csv"
        try:
            client.scrip_master(exchange_segment=seg)
        except: pass

        if not os.path.exists(master_file) or "<html" in open(master_file).read(100).lower():
            url = "https://api.kite.trade/instruments/BFO" if idx_name == "SENSEX" else "https://api.kite.trade/instruments/NFO"
            r = requests.get(url)
            with open(master_file, "wb") as f: f.write(r.content)

        df_raw = pd.read_csv(master_file, low_memory=False)
        all_syms = set(df_raw.iloc[:, 7].astype(str).values) if "exchange_token" not in open(master_file).read(100).lower() else set(df_raw['tradingsymbol'].values)
        
        prefixes = set()
        atm_str1 = f"{int(atm)}CE"
        atm_str2 = f"{int(atm)}.00CE"
        
        for sym in all_syms:
            if sym.startswith(idx_name):
                if sym.endswith(atm_str1):
                    prefixes.add(sym[len(idx_name):-len(atm_str1)])
                elif sym.endswith(atm_str2):
                    prefixes.add(sym[len(idx_name):-len(atm_str2)])
                    
        valid_expiries = []
        today = datetime.now(IST).date()
        
        for p in prefixes:
            try:
                if re.match(r"^\d{2}[A-Z]{3}\d{2}$", p): 
                    dt = datetime.strptime(p, "%d%b%y").date()
                    if dt >= today: valid_expiries.append((dt, p))
                    
                elif re.match(r"^\d{2}[A-Z]{3}$", p): 
                    year = 2000 + int(p[:2])
                    month = datetime.strptime(p[2:], "%b").month
                    last_day = calendar.monthrange(year, month)[1]
                    dt = datetime(year, month, last_day).date()
                    while dt.weekday() != 3: dt -= timedelta(days=1)
                    if dt >= today - timedelta(days=3): 
                        valid_expiries.append((dt, p))
                        
                elif re.match(r"^\d{5}$", p): 
                    y = 2000 + int(p[:2])
                    m = "123456789OND".index(p[2]) + 1
                    d = int(p[3:])
                    dt = datetime(y, m, d).date()
                    if dt >= today: valid_expiries.append((dt, p))
            except: pass
            
        expiry_str = None
        if valid_expiries:
            valid_expiries.sort(key=lambda x: x[0])
            expiry_str = valid_expiries[0][1]

        if expiry_str:
            prefix = f"{idx_name}{expiry_str}"
            coll = get_collection(conf["Coll"])
            await coll.delete_many({}) 
            
            db_records = []
            df = pd.read_csv(master_file, low_memory=False)
            is_kite = "exchange_token" in open(master_file).read(100).lower()

            if is_kite:
                df = df[(df['name'] == idx_name) & (df['tradingsymbol'].str.startswith(prefix))]
                for _, r in df.iterrows():
                    db_records.append({"Token": str(int(r['exchange_token'])), "Symbol": r['tradingsymbol'], "Strike": int(r['strike']), "Type": "CE" if "CE" in r['tradingsymbol'] else "PE"})
            else:
                with open(master_file, "r") as f:
                    for line in f:
                        if prefix in line:
                            parts = line.split(',')
                            token = parts[0].strip()
                            sym = next((p.strip() for p in parts if p.strip().startswith(prefix)), None)
                            if sym and token.isdigit():
                                stk = sym.replace(prefix, "").replace("CE","").replace("PE","").replace(".00","")
                                db_records.append({"Token": token, "Symbol": sym, "Strike": int(float(stk)), "Type": "CE" if "CE" in sym else "PE"})

            if db_records:
                await coll.insert_many(db_records)
                total_loaded += len(db_records)

    return {"status": "success", "message": f"Weekly Expiry Data Updated! Total {total_loaded} strikes saved."}


# =========================================
# 🛠️ ROUTE 1: MANUAL SYNC ENDPOINT
# =========================================
@router.post("/sync-master")
async def sync_master_data_endpoint(current_user: dict = Depends(get_current_user)):
    try:
        return await perform_master_sync(current_user["id"])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================
# ⛓️ ROUTE 2: OPTION CHAIN (From Weekly Coll)
# =========================================
@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
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

        coll = get_collection(conf["Coll"])
        cursor = await coll.find().to_list(length=None)
        if not cursor: raise Exception("No weekly data found. Please click 'Update Expiry'.")

        strike_map = {}
        req_tokens = []
        for doc in cursor:
            stk = doc["Strike"]
            if stk not in strike_map: strike_map[stk] = {"strike": stk, "ce_ltp": 0, "pe_ltp": 0}
            tk = doc["Token"]
            req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
            if doc["Type"] == "CE": strike_map[stk]["ce_token"] = tk
            else: strike_map[stk]["pe_token"] = tk

        for i in range(0, len(req_tokens), 50):
            batch = req_tokens[i:i+50]
            q = client.quotes(instrument_tokens=batch, quote_type="all")
            raw = q if isinstance(q, list) else q.get('data', [])
            for item in raw:
                tk = str(item.get('exchange_token') or item.get('tk'))
                ltp = float(item.get('ltp', 0))
                for s, data in strike_map.items():
                    if data.get("ce_token") == tk: data["ce_ltp"] = ltp
                    if data.get("pe_token") == tk: data["pe_ltp"] = ltp

        sorted_chain = [strike_map[k] for k in sorted(strike_map.keys())]
        return {"status": "success", "spot_price": spot_ltp, "data": sorted_chain}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# =========================================
# ⏰ ROUTE 3: SMART AUTO BACKGROUND SYNC TASK
# =========================================
async def auto_sync_scheduler():
    last_sync_date = None
    while True:
        now = datetime.now(IST)
        
        # Check if today is Friday (4) and time is 9:00 AM OR LATER
        if now.weekday() == 4 and now.time() >= datetime.strptime("09:00", "%H:%M").time():
            current_date = now.strftime("%Y-%m-%d")
            
            # Agar aaj sync nahi hua hai (Catch-Up Logic)
            if last_sync_date != current_date:
                if KOTAK_SESSIONS: # Check if anyone is logged in
                    active_user_id = list(KOTAK_SESSIONS.keys())[0]
                    try:
                        await perform_master_sync(active_user_id)
                        last_sync_date = current_date
                        print(f"✅ AUTO CATCH-UP SYNC EXECUTED ON FRIDAY AT {now.strftime('%H:%M')} FOR {current_date}")
                    except Exception as e:
                        print(f"❌ AUTO SYNC FAILED: {e}")
                        
        await asyncio.sleep(45) 

@router.on_event("startup")
async def start_market_background_tasks():
    asyncio.create_task(auto_sync_scheduler())
