from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
import pandas as pd
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
        raise Exception("Kotak Session is OFF! Please Start Daily Session.")
    return KOTAK_SESSIONS[user_id]

@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"]) 
        conf = INDICES_CONFIG.get(symbol)
        
        # 1. FETCH SPOT PRICE
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

        # 2. ATM CALCULATION
        gap = conf["Gap"]
        ltp_int = int(spot_ltp)
        rem = ltp_int % gap
        atm = (ltp_int - rem) if rem < (gap / 2) else (ltp_int + (gap - rem))
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # 3. MONGODB MASTER & SMART EXPIRY SEARCH
        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        if df.empty or "7" not in df.columns.astype(str): 
            raise Exception("MongoDB Master Data empty.")
            
        df.columns = df.columns.astype(str)
        all_symbols = set(df["7"].astype(str).values)
        now_ist = datetime.now(IST)
        expiry_str = None
        
        # 🟢 SMART LOOP: Har din ke 4 format check karega (Kotak + Kite dono ke liye)
        for i in range(45):
            test_date = now_ist + timedelta(days=i)
            fmt1 = test_date.strftime('%d%b%y').upper() # Kotak: 02APR26
            fmt2 = f"{test_date.day}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}" # Kotak Alt: 2APR26
            m_char = "123456789OND"[test_date.month - 1]
            fmt3 = f"{test_date.strftime('%y')}{m_char}{test_date.strftime('%d')}" # Kite Weekly: 26402
            fmt4 = f"{test_date.strftime('%y')}{test_date.strftime('%b').upper()}" # Kite Monthly: 26APR
            
            for d_str in [fmt1, fmt2, fmt3, fmt4]:
                if f"{symbol}{d_str}{atm}CE" in all_symbols or f"{symbol}{d_str}{atm}.00CE" in all_symbols:
                    expiry_str = d_str
                    break
            if expiry_str: break
                
        if not expiry_str: 
            raise Exception(f"No valid expiry found for ATM {atm}.")

        # 4. EXTRACT TOKENS & FETCH QUOTES
        prefix = f"{symbol}{expiry_str}"
        req_tokens, strike_map = [], {}
        
        for stk in strikes:
            strike_map[stk] = {"strike": stk, "ce_ltp": 0, "ce_oi": 0, "pe_ltp": 0, "pe_oi": 0}
            row_ce = df[(df["7"] == f"{prefix}{stk}.00CE") | (df["7"] == f"{prefix}{stk}CE")]
            if not row_ce.empty:
                tk = str(int(float(row_ce.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[stk]["ce_token"] = tk
                
            row_pe = df[(df["7"] == f"{prefix}{stk}.00PE") | (df["7"] == f"{prefix}{stk}PE")]
            if not row_pe.empty:
                tk = str(int(float(row_pe.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[stk]["pe_token"] = tk

        if not req_tokens: 
            raise Exception("Option tokens mapping failed.")

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

        chain_data = [{"strike": st, **strike_map[st]} for st in strikes]
        
        return {
            "status": "success", "symbol": symbol, "expiry": expiry_str, 
            "spot_price": spot_ltp, "data": chain_data
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
