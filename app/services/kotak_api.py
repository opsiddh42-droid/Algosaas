from app.core.database import get_collection
from datetime import datetime, timedelta
import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Indices ki core configuration
INDICES_CONFIG = {
    "NIFTY": {"exchange": "nse_fo", "lot_size": 65, "strike_gap": 50},
    "BANKNIFTY": {"exchange": "nse_fo", "lot_size": 20, "strike_gap": 100},
    "SENSEX": {"exchange": "bse_fo", "lot_size": 10, "strike_gap": 100}
}

async def generate_option_chain(index_name: str, current_ltp: float, range_strikes: int = 10):
    """
    MongoDB se master contracts utha kar ATM aur Strikes ki Option Chain banayega.
    """
    if index_name not in INDICES_CONFIG:
        return {"status": "error", "message": "Invalid Index"}

    conf = INDICES_CONFIG[index_name]
    atm = round(current_ltp / conf["strike_gap"]) * conf["strike_gap"]
    
    # MongoDB se instrument master data lana (Jo hum daily subah update karenge)
    fo_master_col = get_collection("fo_master")
    cursor = fo_master_col.find({"IndexName": index_name})
    master_data = await cursor.to_list(length=10000)
    
    if not master_data:
        return {"status": "error", "message": "Master Data not found in DB."}

    df = pd.DataFrame(master_data)
    
    # 1. SMART EXPIRY FINDER
    now = datetime.now()
    expiry_date_str = None
    all_ref_keys = set(df["7"].astype(str).values) 
    
    # Agle 45 din mein sabse pehli expiry dhoondhna
    for i in range(0, 45):
        test_date = now + timedelta(days=i)
        d_str = f"{test_date.strftime('%d')}{test_date.strftime('%b').upper()}{test_date.strftime('%y')}"
        
        # Checking for format like 'NIFTY24APR22500.00CE' or 'NIFTY24APR22500CE'
        check_sym_1 = f"{index_name}{d_str}{atm}.00CE"
        check_sym_2 = f"{index_name}{d_str}{atm}CE" 
        
        if check_sym_1 in all_ref_keys or check_sym_2 in all_ref_keys:
            expiry_date_str = d_str
            break
            
    if not expiry_date_str:
        return {"status": "error", "message": f"Expiry Not Found for ATM {atm}"}

    # 2. FILTERING STRIKES (ATM +- range_strikes)
    prefix = f"{index_name}{expiry_date_str}"
    relevant_df = df[df["7"].str.startswith(prefix, na=False)]
    
    strikes = [atm + (i * conf["strike_gap"]) for i in range(-range_strikes, range_strikes + 1)]
    option_chain = []
    
    for _, row in relevant_df.iterrows():
        ref_key = str(row["7"]).strip()
        trd_sym = str(row["5"]).strip()
        token = str(int(float(row["0"])))
        
        for stk in strikes:
            if f"{stk}.00CE" in ref_key or f"{stk}CE" in ref_key:
                 option_chain.append({"trade_symbol": trd_sym, "token": token, "type": "CE", "strike": stk, "exchange": conf["exchange"]})
            elif f"{stk}.00PE" in ref_key or f"{stk}PE" in ref_key:
                 option_chain.append({"trade_symbol": trd_sym, "token": token, "type": "PE", "strike": stk, "exchange": conf["exchange"]})

    # Sort strikes format mein
    option_chain = sorted(option_chain, key=lambda x: (x['strike'], x['type']))

    return {
        "status": "success", 
        "index": index_name,
        "atm": atm,
        "expiry": expiry_date_str,
        "chain": option_chain
    }
