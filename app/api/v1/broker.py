from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

# 🟢 EK LAUTI MEMORY DICTIONARY
from app.core.sessions import KOTAK_SESSIONS

router = APIRouter()

class KotakCredentials(BaseModel):
    name: str; mobile: str; ucc: str; mpin: str; totp: str; consumer_key: str

class KotakTotpOnly(BaseModel):
    totp: str

# Mobile Formatter (+91)
def format_mobile(mobile_no):
    m = str(mobile_no).strip()
    if len(m) == 10: return "+91" + m
    elif m.startswith("91") and len(m) == 12: return "+" + m
    return m

@router.post("/kotak-login")
async def connect_kotak_full(creds: KotakCredentials, current_user: dict = Depends(get_current_user)):
    try:
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        safe_mobile = format_mobile(creds.mobile)
        
        resp1 = client.totp_login(mobile_number=safe_mobile, ucc=creds.ucc, totp=creds.totp)
        resp2 = client.totp_validate(mpin=creds.mpin)
        
        if 'token' not in str(resp2): raise Exception(f"Login Failed! {resp1}")

        # 🟢 ZINDA CLIENT MEMORY MEIN SAVE KIYA
        KOTAK_SESSIONS[current_user["id"]] = client

        users_col = get_collection("users")
        await users_col.update_one({"id": current_user["id"]}, {"$set": {
            "kotak_status": "Active", "kotak_name": creds.name, "kotak_ucc": creds.ucc,
            "kotak_consumer_key": creds.consumer_key, "kotak_mobile": safe_mobile, "kotak_mpin": creds.mpin
        }})
        return {"status": "success", "message": "✅ Kotak Neo Connected!"}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@router.post("/kotak-totp-login")
async def connect_kotak_quick(req: KotakTotpOnly, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})
        if not user_data or user_data.get("kotak_status") != "Active": raise Exception("Broker profile not found.")

        client = NeoAPI(consumer_key=user_data.get("kotak_consumer_key"), environment='prod')
        safe_mobile = format_mobile(user_data.get("kotak_mobile"))
        
        resp1 = client.totp_login(mobile_number=safe_mobile, ucc=user_data.get("kotak_ucc"), totp=req.totp)
        resp2 = client.totp_validate(mpin=user_data.get("kotak_mpin"))
        
        if 'token' not in str(resp2): raise Exception(f"Login Failed! {resp1}")

        # 🟢 ZINDA CLIENT MEMORY MEIN SAVE KIYA
        KOTAK_SESSIONS[current_user["id"]] = client

        return {"status": "success", "message": "✅ Live Trading Session Started!"}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))
