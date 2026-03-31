from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS  # 🟢 MEMORY IMPORT KI

router = APIRouter()

class KotakCredentials(BaseModel):
    name: str; mobile: str; ucc: str; mpin: str; totp: str; consumer_key: str

class KotakTotpOnly(BaseModel):
    totp: str

# 🟢 STRICT CHECKER
def verify_login_success(client):
    try: client.positions()
    except Exception as e:
        raise Exception("TOTP Expired ya MPIN galat hai! Kripya fresh TOTP daalein.")

@router.post("/kotak-login")
async def connect_kotak_full(creds: KotakCredentials, current_user: dict = Depends(get_current_user)):
    try:
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        client.totp_validate(mpin=creds.mpin)
        verify_login_success(client)
        
        KOTAK_SESSIONS[current_user["id"]] = client # 🟢 SAVE IN MEMORY

        users_col = get_collection("users")
        await users_col.update_one({"id": current_user["id"]}, {"$set": {
            "kotak_status": "Active", "kotak_name": creds.name, "kotak_ucc": creds.ucc,
            "kotak_consumer_key": creds.consumer_key, "kotak_mobile": creds.mobile, "kotak_mpin": creds.mpin  
        }})
        return {"status": "success", "message": "✅ Kotak Neo Connected!", "ucc": creds.ucc}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))

@router.post("/kotak-totp-login")
async def connect_kotak_quick(req: KotakTotpOnly, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})
        if not user_data or user_data.get("kotak_status") != "Active": raise Exception("Broker profile not found.")

        client = NeoAPI(consumer_key=user_data.get("kotak_consumer_key"), environment='prod')
        client.totp_login(mobile_number=user_data.get("kotak_mobile"), ucc=user_data.get("kotak_ucc"), totp=req.totp)
        client.totp_validate(mpin=user_data.get("kotak_mpin"))
        verify_login_success(client)

        KOTAK_SESSIONS[current_user["id"]] = client # 🟢 SAVE IN MEMORY

        return {"status": "success", "message": "✅ Live Trading Session Started!"}
    except Exception as e: raise HTTPException(status_code=400, detail=str(e))
