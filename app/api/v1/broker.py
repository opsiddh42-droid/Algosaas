from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

class KotakCredentials(BaseModel):
    name: str; mobile: str; ucc: str; mpin: str; totp: str; consumer_key: str

class KotakTotpOnly(BaseModel):
    totp: str

# ==========================================
# FULL LOGIN (1st Time Setup)
# ==========================================
@router.post("/kotak-login")
async def connect_kotak_full(creds: KotakCredentials, current_user: dict = Depends(get_current_user)):
    try:
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        
        # 🟢 HUM RESPONSE RECORD KAR RAHE HAIN
        resp1 = client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        resp2 = client.totp_validate(mpin=creds.mpin)
        
        # Token Check
        token = getattr(client, "bearer_token", None) or getattr(client, "access_token", None)
        
        if not token:
            # Asli Kotak Error Screen par bhej do!
            raise Exception(f"Kotak API ne Login Reject kar diya! Asli Wajah -> Step 1: {resp1} | Step 2: {resp2}")

        users_col = get_collection("users")
        await users_col.update_one({"id": current_user["id"]}, {"$set": {
            "kotak_status": "Active", "kotak_name": creds.name, "kotak_ucc": creds.ucc,
            "kotak_consumer_key": creds.consumer_key, "kotak_mobile": creds.mobile, "kotak_mpin": creds.mpin,
            "kotak_bearer_token": token
        }})
        return {"status": "success", "message": "✅ Kotak Neo Connected & Token Saved!", "ucc": creds.ucc}
    except Exception as e: 
        raise HTTPException(status_code=400, detail=str(e))

# ==========================================
# DAILY TOTP LOGIN (Quick Setup)
# ==========================================
@router.post("/kotak-totp-login")
async def connect_kotak_quick(req: KotakTotpOnly, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})
        if not user_data or user_data.get("kotak_status") != "Active": raise Exception("Broker profile not found.")

        client = NeoAPI(consumer_key=user_data.get("kotak_consumer_key"), environment='prod')
        
        # 🟢 HUM RESPONSE RECORD KAR RAHE HAIN
        resp1 = client.totp_login(mobile_number=user_data.get("kotak_mobile"), ucc=user_data.get("kotak_ucc"), totp=req.totp)
        resp2 = client.totp_validate(mpin=user_data.get("kotak_mpin"))
        
        # Token Check
        token = getattr(client, "bearer_token", None) or getattr(client, "access_token", None)
        
        if not token:
            # Asli Kotak Error Screen par bhej do!
            raise Exception(f"Kotak API ne Login Reject kar diya! Asli Wajah -> Step 1: {resp1} | Step 2: {resp2}")

        await users_col.update_one({"id": current_user["id"]}, {"$set": {
            "kotak_bearer_token": token
        }})
        
        return {"status": "success", "message": "✅ Token Generated! Session Started."}
    except Exception as e: 
        raise HTTPException(status_code=400, detail=str(e))
