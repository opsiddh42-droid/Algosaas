import os
import requests
import pandas as pd
import re
import calendar
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
# 🛠️ ROUTE 1: SYNC WEEKLY EXPIRY DATA
# =========================================
@router.post("/sync-master")
async def sync_master_data(current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
        total_loaded = 0

        for idx_name, conf in INDICES_CONFIG.items():
            # 1. Fetch Spot to find ATM
            spot_ltp = 0
            try:
                q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
                item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
                spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))
            except: pass
            
            if spot_ltp == 0 and idx_name == "SENSEX": # Sensex Fallback
                q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
                item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
                spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))

            gap = conf["Gap"]
            atm = (int(spot_ltp) - (int(spot_ltp) % gap)) if (int(spot_ltp) % gap) < (gap/2) else (int(spot_ltp) + (gap - (int(spot_ltp) % gap)))

            # 2. Download Master CSV
            seg = conf["Exchange"]
            master_file = f"{seg}_master.csv"
            try:
                client.scrip_master(exchange_segment=seg)
            except: pass

            # Fallback to Kite if needed
            if not os.path.exists(master_file) or "<html" in open(master_file).read(100).lower():
                url = "https://api.kite.trade/instruments/BFO" if idx_name == "SENSEX" else "https://api.kite.trade/instruments/NFO"
                r = requests.get(url)
                with open(master_file, "wb") as f: f.write(r.content)

            # 3. 🚀 SMART EXPIRY DETECTION (WEEKLY & MONTHLY) 🚀
            df_raw = pd.read_csv(master_file, low_memory=False)
            all_syms = set(df_raw.iloc[:, 7].astype(str).values) if "exchange_token" not in open(master_file).read(100).lower() else set(df_raw['tradingsymbol'].values)
            
            prefixes = set()
            atm_str1 = f"{int(atm)}CE"
            atm_str2 = f"{int(atm)}.00CE"
            
            # Step A: Collect all prefixes for the current ATM strike
            for sym in all_syms:
                if sym.startswith(idx_name):
                    if sym.endswith(atm_str1):
                        prefixes.add(sym[len(idx_name):-len(atm_str1)])
                    elif sym.endswith(atm_str2):
                        prefixes.add(sym[len(idx_name):-len(atm_str2)])
                        
            valid_expiries = []
            today = datetime.now(IST).date()
            
            # Step B: Parse all formats and sort them Chronologically
            for p in prefixes:
                try:
                    if re.match(r"^\d{2}[A-Z]{3}\d{2}$", p): # DDMMMYY (Standard Weekly e.g. 09APR26)
                        dt = datetime.strptime(p, "%d%b%y").date()
                        if dt >= today: valid_expiries.append((dt, p))
                        
                    elif re.match(r"^\d{2}[A-Z]{3}$", p): # YYMMM (Monthly e.g. 26APR)
                        year = 2000 + int(p[:2])
                        month = datetime.strptime(p[2:], "%b").month
                        last_day = calendar.monthrange(year, month)[1]
                        dt = datetime(year, month, last_day).date()
                        # Monthly expiry typically last Thursday (or near month end)
                        while dt.weekday() != 3: dt -= timedelta(days=1)
                        if dt >= today - timedelta(days=3): # Buffer for holiday shifts
                            valid_expiries.append((dt, p))
                            
                    elif re.match(r"^\d{5}$", p): # YYM(DD) (Kite Weekly e.g. 26409)
                        y = 2000 + int(p[:2])
                        m = "123456789OND".index(p[2]) + 1
                        d = int(p[3:])
                        dt = datetime(y, m, d).date()
                        if dt >= today: valid_expiries.append((dt, p))
                except: pass
                
            expiry_str = None
            if valid_expiries:
                # Sort by closest date and pick the absolute first one!
                valid_expiries.sort(key=lambda x: x[0])
                expiry_str = valid_expiries[0][1]

            # 4. Filter Exact Expiry Strikes & Save to Mongo
            if expiry_str:
                prefix = f"{idx_name}{expiry_str}"
                coll = get_collection(conf["Coll"])
                await coll.delete_many({}) # Clear old data
                
                db_records = []
                df = pd.read_csv(master_file, low_memory=False)
                is_kite = "exchange_token" in open(master_file).read(100).lower()

                if is_kite:
                    df = df[(df['name'] == idx_name) & (df['tradingsymbol'].str.startswith(prefix))]
                    for _, r in df.iterrows():
                        db_records.append({"Token": str(int(r['exchange_token'])), "Symbol": r['tradingsymbol'], "Strike": int(r['strike']), "Type": "CE" if "CE" in r['tradingsymbol'] else "PE"})
                else:
                    # Kotak format
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
        
        # 1. Latest Spot & ATM
        q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
        spot_ltp = float(item.get('ltp', 0))
        if spot_ltp == 0 and symbol == "SENSEX": # Fallback
            q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
            item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
            spot_ltp = float(item.get('ltp', 0))

        # 2. Get All Strikes from Weekly Collection
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

        # 3. Batch Quotes (LTP/OI)
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
