from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS  # 🟢 MEMORY IMPORT KI
import pandas as pd
from datetime import datetime, timedelta

router = APIRouter()

INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "SpotToken": "NIFTY50", "SpotExch": "nse_cm", "Gap": 50},
    "BANKNIFTY": {"Exchange": "nse_fo", "SpotToken": "26000", "SpotExch": "nse_cm", "Gap": 100},
    "SENSEX": {"Exchange": "bse_fo", "SpotToken": "1", "SpotExch": "bse_cm", "Gap": 100}
}

# 🟢 THE REAL FIX
def get_kotak_client(db_user: dict):
    user_id = db_user.get("id")
    if user_id not in KOTAK_SESSIONS:
        # Agar memory mein login nahi mila toh yeh error frontend par laal dabbe mein aayega
        raise Exception("Kotak Live Session is OFF! Kripya Dashboard par jakar 'Start Daily Session' (TOTP) karein.")
    
    return KOTAK_SESSIONS[user_id] # Pura logged-in client return karega


@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        db_user = await users_col.find_one({"id": current_user["id"]})
        
        if not db_user or db_user.get("kotak_status") != "Active":
            raise Exception("Broker profile not connected. Setup Kotak Neo first.")

        client = get_kotak_client(db_user) # 🟢 MEMORY WALA CLIENT
        conf = INDICES_CONFIG.get(symbol)

        spot_resp = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        spot_price = 0.0
        if spot_resp and isinstance(spot_resp, dict) and 'data' in spot_resp:
            spot_price = float(spot_resp['data'][0].get('ltp', spot_resp['data'][0].get('lastPrice', 0)))

        if spot_price == 0: raise Exception("Kotak API ne Spot Price 0 diya. TOTP expire ho sakta hai.")

        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        
        if df.empty or "7" not in df.columns.astype(str):
            raise Exception("MongoDB Master Data is empty. Please run the script.")

        df.columns = df.columns.astype(str)
        now = datetime.now()
        expiries_found = []
        all_ref_keys = set(df["7"].astype(str).values)
        
        for i in range(0, 30):
            d_str = (now + timedelta(days=i)).strftime('%d%b%y').upper()
            if any(f"{symbol}{d_str}" in s for s in all_ref_keys):
                if d_str not in expiries_found: expiries_found.append(d_str)
        
        if not expiries_found: raise Exception("No Expiry Date found in DB.")
        nearest_expiry = expiries_found[0]

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
             
        q_resp = client.quotes(instrument_tokens=req_tokens, quote_type="all")
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
        print(f"❌ Error: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
