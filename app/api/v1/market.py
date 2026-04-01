from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
import pandas as pd
from datetime import datetime, timedelta, timezone

router = APIRouter()

# 🟢 HARDCODED FUTURE SYMBOLS (Aapke bataye hue!)
INDICES_CONFIG = {
    "NIFTY": {
        "Exchange": "nse_fo", "SpotToken": "256265", "SpotExch": "nse_cm", "Gap": 50, 
        "FutureSymbol": "NIFTY26APRFUT"
    },
    "BANKNIFTY": {
        "Exchange": "nse_fo", "SpotToken": "26000", "SpotExch": "nse_cm", "Gap": 100, 
        "FutureSymbol": "BANKNIFTY26APRFUT"
    },
    "SENSEX": {
        "Exchange": "bse_fo", "SpotToken": "1", "SpotExch": "bse_cm", "Gap": 100, 
        "FutureSymbol": "SENSEX26APRFUT"
    }
}

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
        
        spot_price = 0.0
        
        # =========================================
        # 🟢 STEP 1: DIRECT HARDCODED FUTURE SYMBOL CHECK
        # =========================================
        search_sym = conf["FutureSymbol"]
        print(f"🔍 Searching Hardcoded Future: {search_sym}")
        
        fut_row = df[df["5"] == search_sym]
        
        if not fut_row.empty:
            tk = str(int(float(fut_row.iloc[0]["0"])))
            try:
                q = client.quotes(instrument_tokens=[{"instrument_token": tk, "exchange_segment": conf["Exchange"]}], quote_type="all")
                if q and isinstance(q, dict) and 'data' in q and len(q['data']) > 0:
                    price = float(q['data'][0].get('ltp', q['data'][0].get('lastPrice', 0)))
                    if price > 0:
                        spot_price = price
                        print(f"✅ Future Price Found: {spot_price}")
            except Exception as e:
                print(f"❌ Future Quote Error: {e}")
        else:
            print(f"⚠️ Warning: {search_sym} MongoDB Master Data mein nahi mila!")

        # =========================================
        # 🟢 STEP 2: SPOT PRICE FALLBACK (Agar Future fail ho)
        # =========================================
        if spot_price == 0:
            print(f"⚠️ Future 0 mila for {symbol}, trying Spot Price Fallback...")
            try:
                spot_resp = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
                if spot_resp and isinstance(spot_resp, dict) and 'data' in spot_resp and len(spot_resp['data']) > 0:
                    spot_price = float(spot_resp['data'][0].get('ltp', spot_resp['data'][0].get('lastPrice', 0)))
                    print(f"✅ Spot Price Found: {spot_price}")
            except Exception as e:
                print(f"❌ Spot Error: {e}")

        # Agar dono zero nikle toh error throw karo
        if spot_price == 0: 
            raise Exception(f"Kotak API ne {symbol} ke Future ({search_sym}) aur Spot dono ka LTP 0 diya hai. Ya toh market band hai, ya DB/Token update maang raha hai.")

        # =========================================
        # 🟢 CALCULATE ATM
        # =========================================
        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # =========================================
        # 🟢 SEARCH EXPIRY (IST Time par)
        # =========================================
        now_ist = datetime.now(IST)
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
