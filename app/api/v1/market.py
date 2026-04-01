from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
import pandas as pd
from datetime import datetime, timedelta, timezone

router = APIRouter()

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "FutureSymbol": "NIFTY26APRFUT", "Gap": 50},
    "BANKNIFTY": {"Exchange": "nse_fo", "FutureSymbol": "BANKNIFTY26APRFUT", "Gap": 100},
    "SENSEX": {"Exchange": "bse_fo", "FutureSymbol": "SENSEX26APRFUT", "Gap": 100}
}

IST = timezone(timedelta(hours=5, minutes=30))

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise Exception("RAW ERROR: Kotak Live Session is missing. Please click 'Start Daily Session'.")
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
            raise Exception("RAW ERROR: MongoDB fo_master collection is empty or missing columns.")

        df.columns = df.columns.astype(str)
        
        # =========================================
        # 1. FETCH FUTURE PRICE 
        # =========================================
        search_sym = conf["FutureSymbol"]
        fut_row = df[df["5"] == search_sym]
        
        if fut_row.empty:
            raise Exception(f"RAW ERROR: Future Symbol {search_sym} not found in MongoDB.")

        fut_tk = str(int(float(fut_row.iloc[0]["0"])))
        spot_price = 0.0
        
        try:
            fut_resp = client.quotes(instrument_tokens=[{"instrument_token": fut_tk, "exchange_segment": conf["Exchange"]}], quote_type="all")
        except Exception as e:
            raise Exception(f"RAW API EXCEPTION (Future Quotes): {str(e)}")

        if isinstance(fut_resp, list) and len(fut_resp) > 0:
            spot_price = float(fut_resp[0].get('ltp', fut_resp[0].get('lastPrice', 0)))
        elif isinstance(fut_resp, dict) and 'data' in fut_resp and len(fut_resp['data']) > 0:
            spot_price = float(fut_resp['data'][0].get('ltp', fut_resp['data'][0].get('lastPrice', 0)))

        if spot_price == 0:
            raise Exception(f"RAW API RESPONSE (Future LTP is 0): {fut_resp}")

        # =========================================
        # 2. CALCULATE ATM
        # =========================================
        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # =========================================
        # 3. EXACT WEEKLY EXPIRY SEARCH (Aapka Logic!)
        # =========================================
        now_ist = datetime.now(IST)
        all_symbols = set(df["7"].astype(str).values)
        expiry_date_str = None
        
        # Aaj se lekar agle 45 din tak ek-ek din check karega
        for i in range(0, 45):
            test_date = now_ist + timedelta(days=i)
            # Format banayega: 02APR26, 03APR26, etc.
            d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
            
            check_sym_1 = f"{symbol}{d_str}{atm}.00CE"
            check_sym_2 = f"{symbol}{d_str}{atm}CE"
            
            # Jo bhi sabse pehli date DB mein match ho gayi, wo Weekly Expiry hai!
            if check_sym_1 in all_symbols or check_sym_2 in all_symbols:
                expiry_date_str = d_str
                break
                
        if not expiry_date_str: 
            raise Exception(f"RAW ERROR: No valid expiry found in DB for ATM {atm}.")

        # =========================================
        # 4. BUILD CHAIN TOKENS
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
            raise Exception(f"RAW ERROR: Option tokens list is empty for prefix {prefix}.")

        # =========================================
        # 5. FETCH LIVE PREMIUMS
        # =========================================
        try:
            opt_resp = client.quotes(instrument_tokens=req_tokens, quote_type="all")
        except Exception as e:
            raise Exception(f"RAW API EXCEPTION (Options Quotes): {str(e)}")
             
        items = opt_resp if isinstance(opt_resp, list) else opt_resp.get('data', [])
        
        if not items:
            raise Exception(f"RAW API RESPONSE (Invalid Options Data): {opt_resp}")

        for item in items:
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
