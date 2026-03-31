from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

class KotakCredentials(BaseModel):
    name: str  
    mobile: str
    ucc: str
    mpin: str
    totp: str
    consumer_key: str

class KotakTotpOnly(BaseModel):
    totp: str

@router.post("/kotak-login")
async def connect_kotak_full(creds: KotakCredentials, current_user: dict = Depends(get_current_user)):
    try:
        print(f"🔄 Full Setup: Connecting Kotak Neo for User: {creds.name} (UCC: {creds.ucc})")
        
        # ✅ NAKLI SECRET WALA JUGAAD YAHAN HAI
        client = NeoAPI(
            consumer_key=creds.consumer_key, 
            consumer_secret="dummy_secret_to_bypass", # Library ko fool karne ke liye
            environment='prod'
        )
        
        client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        client.totp_validate(mpin=creds.mpin)

        users_col = get_collection("users")
        await users_col.update_one(
            {"id": current_user["id"]},
            {
                "$set": {
                    "kotak_status": "Active",
                    "kotak_name": creds.name,
                    "kotak_ucc": creds.ucc,
                    "kotak_consumer_key": creds.consumer_key,
                    "kotak_mobile": creds.mobile,
                    "kotak_mpin": creds.mpin  
                }
            }
        )

        return {"status": "success", "message": "✅ Kotak Neo Connected & Profile Saved!", "ucc": creds.ucc}
    
    except Exception as e:
        print(f"❌ Full Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Broker Login Failed: {str(e)}")


@router.post("/kotak-totp-login")
async def connect_kotak_quick(req: KotakTotpOnly, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})

        if not user_data or user_data.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker profile not found.")

        mobile = user_data.get("kotak_mobile")
        ucc = user_data.get("kotak_ucc")
        mpin = user_data.get("kotak_mpin")
        consumer_key = user_data.get("kotak_consumer_key")

        # ✅ NAKLI SECRET WALA JUGAAD YAHAN BHI DALEGA
        client = NeoAPI(
            consumer_key=consumer_key, 
            consumer_secret="dummy_secret_to_bypass", # Library ko fool karne ke liye
            environment='prod'
        )
        
        client.totp_login(mobile_number=mobile, ucc=ucc, totp=req.totp)
        client.totp_validate(mpin=mpin)

        return {"status": "success", "message": "✅ Live Trading Session Started!"}
    
    except Exception as e:
        print(f"❌ Quick Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid TOTP or Login Failed: {str(e)}")
