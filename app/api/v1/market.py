from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
import pandas as pd
from datetime import datetime, timedelta
import math

router = APIRouter()

# 🟢 CONFIGURATIONS (Sirf Strike Gap aur Exchange zaroori hai)
INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100}
}

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise Exception("Kotak Live Session is OFF! Please click 'Start Daily Session' on Dashboard.")
    return KOTAK_SESSIONS[user_id]


@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        # 1. LIVE CLIENT
        client = get_kotak_client(current_user["id"]) 
        conf = INDICES_CONFIG.get(symbol)

        # 2. LOAD MONGODB MASTER DATA (Instead of CSV)
        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        
        if df.empty or "5" not in df.columns.astype(str): 
            raise Exception("MongoDB Master Data empty ya columns missing hain.")

        df.columns = df.columns.astype(str) # Columns string format mein (0, 1, 5, 7 etc)

        # =========================================
        # 🟢 STEP 1: FIND FUTURE TOKEN (Aapka Telegram Logic)
        # =========================================
        now = datetime.now()
        yy = now.strftime("%y")         
        mon = now.strftime("%b").upper() 
        search_sym = f"{symbol}{yy}{mon}FUT" # Eg: NIFTY26APRFUT
        
        fut_row = df[df["5"] == search_sym]
        if fut_row.empty:
            raise Exception(f"Symbol '{search_sym}' MongoDB (Master Data) mein nahi mila.")
            
        fut_token = str(int(float(fut_row.iloc[0]["0"])))

        # =========================================
        # 🟢 STEP 2: GET ATM PRICE (Aapka Telegram Logic)
        # =========================================
        gap = conf["Gap"]
        spot_price = 0.0
        
        # Kotak API hit
        q = client.quotes(instrument_tokens=[{"instrument_token": fut_token, "exchange_segment": conf["Exchange"]}], quote_type="all")
        
        # Parse result
        if q and isinstance(q, dict) and 'data' in q and len(q['data']) > 0:
            spot_price = float(q['data'][0].get('ltp', q['data'][0].get('lastPrice', 0)))
            
        if spot_price == 0: 
            raise Exception(f"Failed to fetch Future Price for {search_sym}. Kotak API returned 0.")

        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # =========================================
        # 🟢 STEP 3: SEARCH EXPIRY (Aapka Telegram Logic)
        # =========================================
        all_symbols = set(df["7"].astype(str).values)
        expiry_date_str = None
        
        for i in range(0, 45):
            test_date = now + timedelta(days=i)
            d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
            
            # Smart check (with .00 and without .00)
            check_sym_1 = f"{symbol}{d_str}{atm}.00CE"
            check_sym_2 = f"{symbol}{d_str}{atm}CE"
            
            if check_sym_1 in all_symbols or check_sym_2 in all_symbols:
                expiry_date_str = d_str
                break
                
        if not expiry_date_str: 
            raise Exception(f"No valid expiry found for ATM {atm}.")

        # =========================================
        # 🟢 STEP 4: BUILD CHAIN TOKENS
        # =========================================
        prefix = f"{symbol}{expiry_date_str}"
        req_tokens = []
        strike_map = {} 
        
        for stk in strikes:
            strike_map[stk] = {"strike": stk, "ce_ltp": 0, "ce_oi": 0, "pe_ltp": 0, "pe_oi": 0}
            
            # CE Token Check
            row_ce = df[(df["7"] == f"{prefix}{stk}.00CE") | (df["7"] == f"{prefix}{stk}CE")]
            if not row_ce.empty:
                tk = str(int(float(row_ce.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[stk]["ce_token"] = tk
                
            # PE Token Check
            row_pe = df[(df["7"] == f"{prefix}{stk}.00PE") | (df["7"] == f"{prefix}{stk}PE")]
            if not row_pe.empty:
                tk = str(int(float(row_pe.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[stk]["pe_token"] = tk

        if not req_tokens: 
            raise Exception(f"Option tokens nahi mile {prefix} ke liye.")
             
        # =========================================
        # 🟢 STEP 5: FETCH LIVE PREMIUMS
        # =========================================
        q_resp = client.quotes(instrument_tokens=req_tokens, quote_type="all")
             
        if q_resp and isinstance(q_resp, dict) and 'data' in q_resp:
            for item in q_resp['data']:
                tk = str(item.get('exchange_token', item.get('tk')))
                for st, data in strike_map.items():
                    if data.get("ce_token") == tk:
                        data["ce_ltp"] = float(item.get('ltp', item.get('lastPrice', 0)))
                        data["ce_oi"] = int(item.get('open_int', item.get('oi', 0)))
                    elif data.get("pe_token") == tk:
                        data["pe_ltp"] = float(item.get('ltp', item.get('lastPrice', 0)))
                        data["pe_oi"] = int(item.get('open_int', item.get('oi', 0)))

        chain_data = [{"strike": st, **strike_map[st]} for st in strikes]
        
        return {
            "status": "success", 
            "symbol": symbol, 
            "expiry": expiry_date_str, 
            "spot_price": spot_price, 
            "data": chain_data, 
            "is_dummy": False
        }

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
