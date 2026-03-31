from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

# --- 1. MODELS ---
class KotakCredentials(BaseModel):
    name: str  
    mobile: str
    ucc: str
    mpin: str
    totp: str
    consumer_key: str

# Naya model sirf TOTP ke liye
class KotakTotpOnly(BaseModel):
    totp: str

# ==========================================
# ROUTE 1: FULL SETUP (Pehli baar ke liye)
# ==========================================
@router.post("/kotak-login")
async def connect_kotak_full(
    creds: KotakCredentials, 
    current_user: dict = Depends(get_current_user)
):
    try:
        print(f"🔄 Full Setup: Connecting Kotak Neo for User: {creds.name} (UCC: {creds.ucc})")
        
        # 1. Kotak Neo Client Initialize aur Login
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        client.totp_validate(mpin=creds.mpin)

        # 2. Database Update (Ab MPIN bhi save kar rahe hain taaki kal kaam aaye)
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
                    "kotak_mpin": creds.mpin  # <-- Naya: MPIN Save kiya
                }
            }
        )

        return {
            "status": "success", 
            "message": "✅ Kotak Neo Connected & Profile Saved!",
            "ucc": creds.ucc
        }
    
    except Exception as e:
        print(f"❌ Full Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Broker Login Failed: {str(e)}")


# ==========================================
# ROUTE 2: DAILY QUICK LOGIN (Sirf TOTP se)
# ==========================================
@router.post("/kotak-totp-login")
async def connect_kotak_quick(
    req: KotakTotpOnly, 
    current_user: dict = Depends(get_current_user)
):
    try:
        # 1. Database se user ki purani details nikalo
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})

        if not user_data or user_data.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker profile not found. Please setup again.")

        mobile = user_data.get("kotak_mobile")
        ucc = user_data.get("kotak_ucc")
        mpin = user_data.get("kotak_mpin")
        consumer_key = user_data.get("kotak_consumer_key")

        if not all([mobile, ucc, mpin, consumer_key]):
            raise HTTPException(status_code=400, detail="Saved data is incomplete. Please re-configure Kotak Neo.")

        print(f"⚡ Quick Login: Connecting Kotak Neo for UCC: {ucc} with fresh TOTP")

        # 2. Kotak Neo Client Initialize aur Login (Purana Data + Naya TOTP)
        client = NeoAPI(consumer_key=consumer_key, environment='prod')
        client.totp_login(mobile_number=mobile, ucc=ucc, totp=req.totp)
        client.totp_validate(mpin=mpin)

        # Yahan par agar aapko token vagaira cache karna ho toh kar sakte hain

        return {
            "status": "success", 
            "message": "✅ Live Trading Session Started!"
        }
    
    except Exception as e:
        print(f"❌ Quick Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Invalid TOTP or Login Failed: {str(e)}")
