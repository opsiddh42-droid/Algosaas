from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
import pandas as pd
from datetime import datetime, timedelta

# 🟢 GLOBAL SESSION STORAGE (Live Token bachane ke liye)
try:
    from app.core.sessions import KOTAK_SESSIONS
except ImportError:
    # Agar sessions.py file nahi bani hai, toh temporary yahi bana dete hain
    KOTAK_SESSIONS = {}

router = APIRouter()

# --- ⚙️ CONFIGURATION ---
INDICES_CONFIG = {
    "NIFTY": {"Exchange": "nse_fo", "SpotToken": "NIFTY50", "SpotExch": "nse_cm", "Gap": 50},
    "BANKNIFTY": {"Exchange": "nse_fo", "SpotToken": "26000", "SpotExch": "nse_cm", "Gap": 100},
    "SENSEX": {"Exchange": "bse_fo", "SpotToken": "1", "SpotExch": "bse_cm", "Gap": 100}
}

# 🟢 NAYA CLIENT FETCHER (Memory se uthayega)
def get_kotak_client(db_user: dict):
    user_id = db_user.get("id")
    if user_id in KOTAK_SESSIONS:
        return KOTAK_SESSIONS[user_id]
    else:
        # Agar session nahi hai, toh seedha error throw karega
        raise Exception("Active Kotak Session Not Found! Please go to Dashboard and start Daily Session (TOTP).")


# ==========================================
# ROUTE: GET REAL OPTION CHAIN (MONGODB MASTER)
# ==========================================
@router.get("/option-chain")
async def get_option_chain(symbol: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        db_user = await users_col.find_one({"id": current_user["id"]})
        
        if not db_user or db_user.get("kotak_status") != "Active":
            raise Exception("Broker profile not connected. Please setup Kotak Neo first.")

        # Client memory se nikal rahe hain
        client = get_kotak_client(db_user)

        conf = INDICES_CONFIG.get(symbol)
        if not conf:
            raise Exception("Invalid Index Symbol.")

        # 1. GET REAL SPOT PRICE
        spot_resp = client.quotes(instrument_tokens=[{"instrument_token": conf["SpotToken"], "exchange_segment": conf["SpotExch"]}], quote_type="all")
        spot_price = 0.0
        if spot_resp and isinstance(spot_resp, dict) and 'data' in spot_resp:
            spot_price = float(spot_resp['data'][0].get('ltp', spot_resp['data'][0].get('lastPrice', 0)))

        if spot_price == 0:
            raise Exception(f"Failed to fetch Live Spot Price from Kotak for token {conf['SpotToken']}. Check API/Token.")

        # 2. CALCULATE ATM & STRIKE RANGE
        gap = conf["Gap"]
        atm = round(spot_price / gap) * gap
        strikes = [atm + (i * gap) for i in range(-10, 11)]

        # 3. 🚀 FETCH FROM MONGODB `fo_master`
        fo_master_col = get_collection("fo_master")
        cursor = await fo_master_col.find({"IndexName": symbol}).to_list(length=None)
        df = pd.DataFrame(cursor)
        
        if df.empty or "7" not in df.columns.astype(str):
            raise Exception("Master Data empty or invalid in MongoDB. Please run the data uploader script.")

        df.columns = df.columns.astype(str)
        now = datetime.now()
        expiries_found = []
        all_ref_keys = set(df["7"].astype(str).values)
        
        # Agle 30 din mein jo bhi pehli expiry milegi
        for i in range(0, 30):
            test_date = now + timedelta(days=i)
            d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
            check_sym = f"{symbol}{d_str}"
            if any(check_sym in s for s in all_ref_keys):
                if d_str not in expiries_found:
                    expiries_found.append(d_str)
        
        if not expiries_found:
            raise Exception("No upcoming expiry found in database.")
            
        nearest_expiry = expiries_found[0]

        # 4. GATHER TOKENS
        req_tokens = []
        strike_map = {} 
        
        for st in strikes:
            strike_map[st] = {"strike": st, "ce_ltp": 0, "ce_oi": 0, "pe_ltp": 0, "pe_oi": 0}
            
            # CE
            ce_sym_1 = f"{symbol}{nearest_expiry}{st}.00CE"
            ce_sym_2 = f"{symbol}{nearest_expiry}{st}CE"
            match_ce = df[(df["7"] == ce_sym_1) | (df["7"] == ce_sym_2)]
            if not match_ce.empty:
                tk = str(int(float(match_ce.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[st]["ce_token"] = tk
                
            # PE
            pe_sym_1 = f"{symbol}{nearest_expiry}{st}.00PE"
            pe_sym_2 = f"{symbol}{nearest_expiry}{st}PE"
            match_pe = df[(df["7"] == pe_sym_1) | (df["7"] == pe_sym_2)]
            if not match_pe.empty:
                tk = str(int(float(match_pe.iloc[0]["0"])))
                req_tokens.append({"instrument_token": tk, "exchange_segment": conf["Exchange"]})
                strike_map[st]["pe_token"] = tk

        # 5. FETCH REAL QUOTES
        if not req_tokens:
             raise Exception("Option tokens not found in Master Data for calculated strikes.")
             
        q_resp = client.quotes(instrument_tokens=req_tokens, quote_type="all")
        
        if q_resp and isinstance(q_resp, dict) and 'data' in q_resp:
            for item in q_resp['data']:
                tk = str(item.get('exchange_token', item.get('tk')))
                ltp = float(item.get('ltp', item.get('lastPrice', 0)))
                oi = int(item.get('open_int', item.get('oi', 0)))
                
                for st, data in strike_map.items():
                    if data.get("ce_token") == tk:
                        data["ce_ltp"] = ltp
                        data["ce_oi"] = oi
                    elif data.get("pe_token") == tk:
                        data["pe_ltp"] = ltp
                        data["pe_oi"] = oi

        # 6. RETURN
        chain_data = [{"strike": st, **strike_map[st]} for st in strikes]
        return {
            "status": "success", "symbol": symbol, "expiry": nearest_expiry,
            "spot_price": spot_price, "data": chain_data, "is_dummy": False
        }

    except Exception as e:
        print(f"❌ Real Option Chain Error: {str(e)}")
        # Frontend ko saaf error bhej rahe hain
        raise HTTPException(status_code=400, detail=str(e))
