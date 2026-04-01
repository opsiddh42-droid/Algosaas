import pandas as pd
import os
import requests
from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
from datetime import datetime, timedelta, timezone

router = APIRouter()
IST = timezone(timedelta(hours=5, minutes=30))

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50, "SpotToken": "Nifty 50", "SpotExch": "nse_cm"},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100, "SpotToken": "Nifty Bank", "SpotExch": "nse_cm"},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100, "SpotToken": "SENSEX", "SpotExch": "nse_cm"} 
}

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise HTTPException(status_code=401, detail="Kotak Session is OFF! Please Start Daily Session.")
    return KOTAK_SESSIONS[user_id]

# =========================================
# 🛠️ ROUTE 1: THE BULLETPROOF MASTER SYNC
# =========================================
@router.post("/sync-master")
async def sync_master_data(current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
        fo_master_col = get_collection("fo_master")
        await fo_master_col.delete_many({}) # Purana data saaf
        
        indices = ["NIFTY", "BANKNIFTY", "SENSEX"]
        total_inserted = 0

        for idx_name in indices:
            seg = "bse_fo" if idx_name == "SENSEX" else "nse_fo"
            master_file = f"{seg}_master.csv"
            temp_file = f"{seg}.csv"
            
            print(f"📥 Requesting {master_file} from Kotak API for {idx_name}...")
            try:
                client.scrip_master(exchange_segment=seg)
            except Exception as e:
                print(f"  ⚠️ Kotak API Error: {e}")
                
            target_file = None
            if os.path.exists(temp_file): target_file = temp_file
            elif os.path.exists(master_file): target_file = master_file
            
            is_fallback = False
            
            # Check for Kotak HTML Error Page
            if target_file:
                with open(target_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(150).lower()
                if "<html" in content or "page not found" in content:
                    print(f"❌ Kotak returned HTML error for {idx_name}. Switching to Fallback...")
                    is_fallback = True
                else:
                    if target_file == temp_file:
                        if os.path.exists(master_file): os.remove(master_file)
                        os.rename(temp_file, master_file)
            else:
                is_fallback = True

            # 🟢 FALLBACK DOWNLOADER (Kite API)
            if is_fallback:
                url = "https://api.kite.trade/instruments/BFO" if idx_name == "SENSEX" else "https://api.kite.trade/instruments/NFO"
                try:
                    r = requests.get(url)
                    if r.status_code == 200:
                        with open(master_file, "wb") as f:
                            f.write(r.content)
                        print(f"  ✅ Fallback Master downloaded for {idx_name}!")
                    else:
                        continue
                except Exception as e:
                    print(f"  ❌ Fallback error: {e}"); continue

            # 🟢 PARSE AND UPLOAD TO MONGODB
            db_records = []
            with open(master_file, "r", encoding="utf-8", errors="ignore") as f:
                header = f.readline().lower()
                is_kite_format = "exchange_token" in header

            if is_kite_format:
                df = pd.read_csv(master_file)
                # Kite naming mapping
                k_name = "NIFTY" if idx_name == "NIFTY" else "BANKNIFTY" if idx_name == "BANKNIFTY" else "SENSEX"
                df = df[df['name'] == k_name]
                for _, r in df.iterrows():
                    sym = str(r['tradingsymbol'])
                    if "CE" in sym or "PE" in sym:
                        db_records.append({
                            "IndexName": idx_name,
                            "Token": str(int(r['exchange_token'])),
                            "Symbol": sym,
                            "Strike": int(r['strike']),
                            "Type": "CE" if "CE" in sym else "PE",
                            "Expiry": str(r['expiry']) # Format: 2026-04-02
                        })
            else:
                # Kotak Format Parser
                with open(master_file, "r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if idx_name not in line: continue
                        parts = line.split(',')
                        if len(parts) < 2: continue
                        token = parts[0].strip()
                        if not token.isdigit(): continue
                        
                        sym = None
                        for p in parts:
                            p = p.strip()
                            if p.startswith(idx_name) and ("CE" in p or "PE" in p):
                                sym = p; break
                                
                        if sym:
                            try:
                                # Extract Strike from Symbol (e.g. NIFTY02APR2622900CE)
                                # This is a simple logic, works best when DB filters by prefix later
                                db_records.append({
                                    "IndexName": idx_name,
                                    "Token": token,
                                    "Symbol": sym,
                                    "Type": "CE" if "CE" in sym else "PE"
                                })
                            except: pass

            if db_records:
                await fo_master_col.insert_many(db_records)
                total_inserted += len(db_records)
                print(f"✅ Inserted {len(db_records)} records for {idx_name} to DB.")

        return {"status": "success", "message": f"Master Data Synced! {total_inserted} contracts loaded."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# =========================================
# 📈 ROUTE 2: GET FULL OPTION CHAIN
# =========================================
@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"]) 
        conf = INDICES_CONFIG.get(symbol)
        
        # 1. FETCH SPOT PRICE (Aapka SENSEX Fallback Logic)
        spot_ltp = 0
        if symbol == "SENSEX":
            try:
                q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "nse_cm"}], quote_type="all")
                item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
                spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))
            except: pass
            if spot_ltp == 0:
                try:
                    q = client.quotes(instrument_tokens=[{"instrument_token": "SENSEX", "exchange_segment": "bse_cm"}], quote_type="all")
                    item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
                    spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))
                except: pass
        else:
            try:
                q = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
                item = q[0] if isinstance(q, list) else q.get('data', [{}])[0]
                spot_ltp = float(item.get('ltp', item.get('lastPrice', 0)))
            except: pass

        if spot_ltp == 0:
            raise Exception(f"Could not fetch {symbol} Spot Price.")

        # 2. ATM CALCULATION (Aapki Exact Math)
        gap = conf["Gap"]
        ltp_int = int(spot_ltp)
        rem = ltp_int % gap
        atm = (ltp_int - rem) if rem < (gap / 2) else (ltp_int + (gap - rem))

        # 3. GET DB TOKENS
        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        if df.empty: 
            raise Exception("Master Data empty. Please run /sync-master API first.")
            
        now_ist = datetime.now(IST)
        all_symbols = set(df["Symbol"].values)
        expiry_str = None
        
        # SMART EXPIRY SEARCH
        for i in range(45):
            test_date = now_ist + timedelta(days=i)
            fmt1 = test_date.strftime('%d%b%y').upper() # Kotak: 02APR26
            fmt2 = f"{test_date.day}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}" # Kotak: 2APR26
            m_char = "123456789OND"[test_date.month - 1]
            fmt3 = f"{test_date.strftime('%y')}{m_char}{test_date.strftime('%d')}" # Kite: 26402
            
            for d_str in [fmt1, fmt2, fmt3]:
                if f"{symbol}{d_str}{atm}CE" in all_symbols or f"{symbol}{d_str}{atm}.00CE" in all_symbols:
                    expiry_str = d_str
                    break
            if expiry_str: break
                
        if not expiry_str: 
            raise Exception(f"No valid expiry found for ATM {atm}.")

        # 4. EXTRACT ALL STRIKES FOR THIS EXPIRY (No -10/+10 Limit!)
        prefix = f"{symbol}{expiry_str}"
        target_options = df[df["Symbol"].str.startswith(prefix)].copy()
        
        # Extract Strikes
        target_options["StrikeVal"] = target_options["Symbol"].str.replace(prefix, "", regex=False).str.replace("CE", "", regex=False).str.replace("PE", "", regex=False).str.replace(".00", "", regex=False)
        target_options["StrikeVal"] = pd.to_numeric(target_options["StrikeVal"], errors='coerce')
        target_options = target_options.dropna(subset=['StrikeVal'])
        
        all_strikes_list = sorted(target_options["StrikeVal"].unique())
        
        req_tokens = []
        strike_map = {}
        for stk in all_strikes_list:
            strike_map[stk] = {"strike": stk, "ce_ltp": 0, "ce_oi": 0, "pe_ltp": 0, "pe_oi": 0}

        for _, row in target_options.iterrows():
            stk = row["StrikeVal"]
            tk = row["Token"]
            typ = row["Type"]
            req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
            if typ == "CE": strike_map[stk]["ce_token"] = tk
            else: strike_map[stk]["pe_token"] = tk

        # 5. FETCH LIVE QUOTES IN BATCHES OF 50
        for i in range(0, len(req_tokens), 50):
            batch = req_tokens[i : i+50]
            try:
                q = client.quotes(instrument_tokens=batch, quote_type="all")
                raw = q if isinstance(q, list) else q.get('data', [])
                for item in raw:
                    tk = str(item.get('exchange_token') or item.get('tk'))
                    ltp = float(item.get('ltp', item.get('lastPrice', 0)))
                    oi = int(float(item.get('open_int') or item.get('oi') or 0))
                    
                    for st, data in strike_map.items():
                        if data.get("ce_token") == tk: data["ce_ltp"] = ltp; data["ce_oi"] = oi
                        elif data.get("pe_token") == tk: data["pe_ltp"] = ltp; data["pe_oi"] = oi
            except: pass

        # Return All Strikes
        chain_data = [{"strike": st, **strike_map[st]} for st in all_strikes_list]
        
        return {
            "status": "success", "symbol": symbol, "expiry": expiry_str, 
            "spot_price": spot_ltp, "data": chain_data
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
