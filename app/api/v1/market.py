from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS

import pandas as pd
from datetime import datetime, timedelta

router = APIRouter()

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
        # 1. ZINDA CLIENT LEY AAYE
        client = get_kotak_client(current_user["id"]) 
        conf = INDICES_CONFIG.get(symbol)

        # 2. 🚀 FETCH FROM MONGODB FIRST (Taki Future ka token dhoondh sakein)
        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        
        if df.empty or "5" not in df.columns.astype(str): 
            raise Exception("MongoDB Master Data empty ya theek se upload nahi hua.")

        df.columns = df.columns.astype(str)
        
        # 3. 🟢 TELEGRAM BOT LOGIC: Future Price nikalna hai Spot ki jagah
        now = datetime.now()
        yy = now.strftime("%y")
        mon = now.strftime("%b").upper()
        search_sym = f"{symbol}{yy}{mon}FUT" # Jaise: NIFTY26APRFUT
        
        fut_row = df[df["5"] == search_sym]
        if fut_row.empty:
            raise Exception(f"Master Data mein Future Symbol '{search_sym}' nahi mila!")
            
        fut_token = str(int(float(fut_row.iloc[0]["0"])))
        
        # 4. Kotak API se Live Quote Mangna
        try:
            spot_resp = client.quotes(instrument_tokens=[{"instrument_token": fut_token, "exchange_segment": conf["Exchange"]}], quote_type="all")
        except Exception as e:
            raise Exception(f"Kotak Server Quote Error: {str(e)}")

        spot_price = 0.0
        if spot_resp and isinstance(spot_resp, dict) and 'data' in spot_resp:
            if len(spot_resp['data']) > 0:
                spot_price = float(spot_resp['data'][0].get('ltp', spot_resp['data'][0].get('lastPrice', 0)))

        # 🟢 Asli Check: Agar phir bhi 0 aaya, toh Kotak ka Raw Response print kardo!
        if spot_price == 0: 
            raise Exception(f"Kotak API ne Live Price 0 diya. Raw Response: {spot_resp}")

        # 5. ATM & STRIKES CALCULATE
        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # 6. FIND EXPIRY
        expiries_found = []
        all_ref_keys = set(df["7"].astype(str).values)
        
        for i in range(0, 30):
            d_str = (now + timedelta(days=i)).strftime('%d%b%y').upper()
            if any(f"{symbol}{d_str}" in s for s in all_ref_keys):
                if d_str not in expiries_found: expiries_found.append(d_str)
        
        if not expiries_found: raise Exception("No Expiry Date found in DB.")
        nearest_expiry = expiries_found[0]

        # 7. MAP TOKENS
        req_tokens = []; strike_map = {} 
        for st in strikes:
            strike_map[st] = {"strike": st, "ce_ltp": 0, "ce_oi": 0, "pe_ltp": 0, "pe_oi": 0}
            match_ce = df[(df["7"] == f"{symbol}{nearest_expiry}{st}.00CE") | (df["7"] == f"{symbol}{nearest_expiry}{st}CE")]
            if not match_ce.empty:
                tk = str(int(float(match_ce.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[st]["ce_token"] = tk
                
            match_pe = df[(df["7"] == f"{symbol}{nearest_expiry}{st}.00PE") | (df["7"] == f"{symbol}{nearest_expiry}{st}PE")]
            if not match_pe.empty:
                tk = str(int(float(match_pe.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[st]["pe_token"] = tk

        if not req_tokens: raise Exception("Option tokens match nahi hue.")
             
        # 8. FETCH OPTION CHAIN QUOTES
        try:
             q_resp = client.quotes(instrument_tokens=req_tokens, quote_type="all")
        except Exception as e:
             raise Exception(f"Option Chain Quote Error: {str(e)}")
             
        if q_resp and isinstance(q_resp, dict) and 'data' in q_resp:
            for item in q_resp['data']:
                tk = str(item.get('exchange_token', item.get('tk')))
                for st, data in strike_map.items():
                    if data.get("ce_token") == tk:
                        data["ce_ltp"] = float(item.get('ltp', 0)); data["ce_oi"] = int(item.get('open_int', 0))
                    elif data.get("pe_token") == tk:
                        data["pe_ltp"] = float(item.get('ltp', 0)); data["pe_oi"] = int(item.get('open_int', 0))

        chain_data = [{"strike": st, **strike_map[st]} for st in strikes]
        return {"status": "success", "symbol": symbol, "expiry": nearest_expiry, "spot_price": spot_price, "data": chain_data, "is_dummy": False}

    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
