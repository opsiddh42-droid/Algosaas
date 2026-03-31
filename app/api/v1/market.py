import os
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

# --- 1. CONFIGURATION (Same as your Bot) ---
INDICES_CONFIG = {
    "NIFTY": {
        "Exchange": "nse_fo", "LotSize": 25, "StrikeGap": 50,  # Note: Nifty Lot Size is 25 now
        "MasterFile": "nse_fo_master.csv", 
        "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/nse_fo"
    },
    "BANKNIFTY": {
        "Exchange": "nse_fo", "LotSize": 15, "StrikeGap": 100,
        "MasterFile": "nse_fo_master.csv", 
        "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/nse_fo"
    },
    "SENSEX": {
        "Exchange": "bse_fo", "LotSize": 10, "StrikeGap": 100,
        "MasterFile": "bse_fo_master.csv", 
        "Url": "https://lapi.kotaksecurities.com/wso2-scrip-master/api/v1/scrip-master/csv/bse_fo"
    }
}

# In-memory cache to keep Kotak connections alive (Prevents account block)
ACTIVE_CLIENTS = {}

# --- 2. HELPER FUNCTIONS ---
def get_kotak_client(user_id: str, db_user: dict):
    """Returns an active Kotak client for the user."""
    if user_id in ACTIVE_CLIENTS:
        return ACTIVE_CLIENTS[user_id]
        
    # If not in cache, create a new connection
    try:
        client = NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
        # Here we assume the session is already active from the /kotak-totp-login route.
        # NeoAPI library stores session tokens internally or in a local file.
        # If needed, we perform a silent re-login
        client.totp_login(
            mobile_number=db_user["kotak_mobile"], 
            ucc=db_user["kotak_ucc"], 
            totp="123456" # Dummy TOTP, we rely on the session token established earlier
        )
        ACTIVE_CLIENTS[user_id] = client
        return client
    except Exception as e:
        # If session expired, we'll need fresh TOTP from user
        raise HTTPException(status_code=401, detail="Broker session expired. Please Re-login with TOTP.")

def get_master_csv(conf):
    """Downloads Master CSV if it doesn't exist or is older than 1 day."""
    file_path = conf["MasterFile"]
    if not os.path.exists(file_path) or (time.time() - os.path.getmtime(file_path) > 86400):
        try:
            r = requests.get(conf["Url"])
            with open(file_path, 'wb') as f: f.write(r.content)
        except Exception as e:
            print(f"Error downloading CSV: {e}")
            
    # Load and return DataFrame
    df = pd.read_csv(file_path, sep=',', header=None, low_memory=False)
    return df

# --- 3. MAIN OPTION CHAIN ROUTE ---
@router.get("/option-chain")
async def get_live_option_chain(index: str = "NIFTY", current_user: dict = Depends(get_current_user)):
    try:
        if index not in INDICES_CONFIG:
            raise HTTPException(status_code=400, detail="Invalid Index selected")
            
        conf = INDICES_CONFIG[index]
        
        # 1. Fetch User Data & Kotak Client
        users_col = get_collection("users")
        db_user = await users_col.find_one({"id": current_user["id"]})
        if not db_user or db_user.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker not connected")
            
        client = get_kotak_client(current_user["id"], db_user)
        
        # 2. Get Master CSV Data
        df = get_master_csv(conf)
        
        # 3. Find Future to calculate ATM
        now = datetime.now()
        yy = now.strftime("%y")
        mon = now.strftime("%b").upper()
        search_sym = f"{index}{yy}{mon}FUT"
        
        row = df[df[5] == search_sym]
        if row.empty:
            raise HTTPException(status_code=404, detail="Future contract not found for ATM calculation")
            
        fut_token = str(int(row.iloc[0, 0]))
        
        # Get Live Future Price
        q = client.quotes(instrument_tokens=[{"instrument_token": fut_token, "exchange_segment": conf["Exchange"]}], quote_type="all")
        ltp = 0
        if q and isinstance(q, dict) and 'data' in q:
            ltp = float(q['data'][0].get('ltp', 0))
        elif q and isinstance(q, list):
            ltp = float(q[0].get('ltp', 0))
            
        if ltp == 0:
            raise HTTPException(status_code=500, detail="Future LTP is 0, Market might be closed")
            
        # Calculate ATM
        atm = round(ltp / conf["StrikeGap"]) * conf["StrikeGap"]
        
        # 4. Find Nearest Expiry Date (Checking next 45 days)
        expiry_date_str = None
        all_ref_keys = set(df[7].astype(str).values) 
        
        for i in range(0, 45):
            test_date = now + timedelta(days=i)
            d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
            
            # Format: NIFTY24APR22000.00CE
            check_sym = f"{index}{d_str}{atm}.00CE"
            if check_sym in all_ref_keys:
                expiry_date_str = d_str
                break
                
        if not expiry_date_str:
            raise HTTPException(status_code=404, detail="Could not find current expiry")

        # 5. Build Strikes (± 10 strikes around ATM to save bandwidth)
        prefix = f"{index}{expiry_date_str}"
        relevant = df[df[7].str.startswith(prefix, na=False)]
        strikes = [atm + (i * conf["StrikeGap"]) for i in range(-10, 11)]
        
        tokens_list = []
        chain_map = {stk: {"strike": stk, "ce_ltp": 0, "ce_oi": 0, "ce_chng": 0, "pe_ltp": 0, "pe_oi": 0, "pe_chng": 0} for stk in strikes}
        
        for _, r in relevant.iterrows():
            ref_key = str(r[7]).strip()
            token = str(int(r[0]))
            
            for stk in strikes:
                if f"{stk}.00CE" in ref_key:
                    chain_map[stk]["ce_token"] = token
                    tokens_list.append({"instrument_token": token, "exchange_segment": conf["Exchange"]})
                elif f"{stk}.00PE" in ref_key:
                    chain_map[stk]["pe_token"] = token
                    tokens_list.append({"instrument_token": token, "exchange_segment": conf["Exchange"]})

        # 6. Fetch Live Quotes in Bulk
        if tokens_list:
            live_quotes = client.quotes(instrument_tokens=tokens_list, quote_type="all")
            quote_data = live_quotes.get('data', []) if isinstance(live_quotes, dict) else live_quotes
            
            live_map = {}
            for item in quote_data:
                tk = str(item.get('exchange_token') or item.get('tk'))
                live_map[tk] = {
                    'ltp': float(item.get('ltp', item.get('lastPrice', 0))),
                    'oi': int(item.get('open_int') or item.get('openInterest') or item.get('oi') or 0),
                    'chng': float(item.get('netChange') or item.get('change') or 0)
                }
                
            # Map Live data back to our chain
            for stk in strikes:
                ce_tk = chain_map[stk].get("ce_token")
                pe_tk = chain_map[stk].get("pe_token")
                
                if ce_tk and ce_tk in live_map:
                    chain_map[stk]["ce_ltp"] = live_map[ce_tk]["ltp"]
                    chain_map[stk]["ce_oi"] = live_map[ce_tk]["oi"]
                    chain_map[stk]["ce_chng"] = live_map[ce_tk]["chng"]
                    
                if pe_tk and pe_tk in live_map:
                    chain_map[stk]["pe_ltp"] = live_map[pe_tk]["ltp"]
                    chain_map[stk]["pe_oi"] = live_map[pe_tk]["oi"]
                    chain_map[stk]["pe_chng"] = live_map[pe_tk]["chng"]

        # 7. Final Output Format for Frontend
        final_options = [chain_map[stk] for stk in strikes]
        
        return {
            "atm": atm,
            "expiry": expiry_date_str,
            "options": final_options
        }
        
    except Exception as e:
        print(f"Option Chain Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
