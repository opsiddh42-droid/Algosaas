from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
import pandas as pd
from datetime import datetime, timedelta, timezone

router = APIRouter()

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "Gap": 50},
    "BANKNIFTY": {"Exchange": "nse_fo", "Gap": 100},
    "SENSEX": {"Exchange": "bse_fo", "Gap": 100}
}

# 🟢 INDIA TIMEZONE FIX (UTC + 5:30)
IST = timezone(timedelta(hours=5, minutes=30))

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise Exception("Kotak Live Session is OFF! Please click 'Start Daily Session' on Dashboard.")
    return KOTAK_SESSIONS[user_id]


@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"]) 
        conf = INDICES_CONFIG.get(symbol)

        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        
        if df.empty or "5" not in df.columns.astype(str): 
            raise Exception("MongoDB Master Data empty ya columns missing hain.")

        df.columns = df.columns.astype(str)
        
        # 🟢 SMART FUTURE FINDER (IST TIME + MONTH ROLLOVER)
        now_ist = datetime.now(IST)
        
        # Hum 2 mahine test karenge: Current aur Next (agar current expire ho gaya ho)
        next_month_date = now_ist.replace(day=28) + timedelta(days=5)
        months_to_try = [now_ist, next_month_date]
        
        spot_price = 0.0
        fut_token = None
        search_sym = ""
        
        for test_date in months_to_try:
            yy = test_date.strftime("%y")         
            mon = test_date.strftime("%b").upper() 
            search_sym = f"{symbol}{yy}{mon}FUT" # Pehle MAR check karega, fail hua toh APR
            
            fut_row = df[df["5"] == search_sym]
            if fut_row.empty:
                continue
                
            tk = str(int(float(fut_row.iloc[0]["0"])))
            
            # API hit karke check karo kya yeh future zinda hai?
            try:
                q = client.quotes(instrument_tokens=[{"instrument_token": tk, "exchange_segment": conf["Exchange"]}], quote_type="all")
                if q and isinstance(q, dict) and 'data' in q and len(q['data']) > 0:
                    price = float(q['data'][0].get('ltp', q['data'][0].get('lastPrice', 0)))
                    if price > 0:
                        spot_price = price
                        fut_token = tk
                        break # Valid price mil gaya, loop tod do!
            except Exception:
                pass

        if spot_price == 0: 
            raise Exception(f"Failed to fetch Future Price. Checked symbols like {search_sym}. Kotak API returned 0.")

        # =========================================
        # 🟢 GET ATM PRICE
        # =========================================
        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # =========================================
        # 🟢 SEARCH EXPIRY (IST Time par)
        # =========================================
        all_symbols = set(df["7"].astype(str).values)
        expiry_date_str = None
        
        for i in range(0, 45):
            test_date = now_ist + timedelta(days=i)
            d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
            
            check_sym_1 = f"{symbol}{d_str}{atm}.00CE"
            check_sym_2 = f"{symbol}{d_str}{atm}CE"
            
            if check_sym_1 in all_symbols or check_sym_2 in all_symbols:
                expiry_date_str = d_str
                break
                
        if not expiry_date_str: 
            raise Exception(f"No valid expiry found for ATM {atm}.")

        # =========================================
        # 🟢 BUILD CHAIN TOKENS
        # =========================================
        prefix = f"{symbol}{expiry_date_str}"
        req_tokens = []
        strike_map = {} 
        
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
            raise Exception(f"Option tokens nahi mile {prefix} ke liye.")
             
        # =========================================
        # 🟢 FETCH LIVE PREMIUMS
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
