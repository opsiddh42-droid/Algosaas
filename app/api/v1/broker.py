from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

# 🟢 GLOBAL SESSION STORAGE IMPORT
try:
    from app.core.sessions import KOTAK_SESSIONS
except ImportError:
    import app.api.v1.market as mkt
    KOTAK_SESSIONS = mkt.KOTAK_SESSIONS

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

# --- HELPER: STRICT LOGIN VERIFICATION ---
def verify_login_success(client):
    try:
        # Hum ek dummy call (positions) karke check kar rahe hain. 
        # Agar token None hua, toh yahi par "NoneType" error aakar pakda jayega!
        client.positions()
    except Exception as e:
        if "NoneType" in str(e) or "Bearer" in str(e):
            raise Exception("TOTP Expired ya MPIN galat hai! Please Authenticator App se fresh TOTP daalein.")
        raise Exception(f"Kotak Server Error: {str(e)}")

# ==========================================
# FULL LOGIN (1st Time Setup)
# ==========================================
@router.post("/kotak-login")
async def connect_kotak_full(creds: KotakCredentials, current_user: dict = Depends(get_current_user)):
    try:
        print(f"🔄 Full Setup: Connecting Kotak Neo for User: {creds.name} (UCC: {creds.ucc})")
        
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        
        # Login attempts
        client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        client.totp_validate(mpin=creds.mpin)

        # 🟢 STRICT CHECK: Kya sach mein login hua?
        verify_login_success(client)

        # Agar error nahi aaya, matlab login successful! Memory mein save karo.
        KOTAK_SESSIONS[current_user["id"]] = client

        users_col = get_collection("users")
        await users_col.update_one(
            {"id": current_user["id"]},
            {"$set": {
                "kotak_status": "Active", "kotak_name": creds.name, "kotak_ucc": creds.ucc,
                "kotak_consumer_key": creds.consumer_key, "kotak_mobile": creds.mobile, "kotak_mpin": creds.mpin  
            }}
        )

        return {"status": "success", "message": "✅ Kotak Neo Connected & Profile Saved!", "ucc": creds.ucc}
    
    except Exception as e:
        print(f"❌ Full Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))


# ==========================================
# DAILY TOTP LOGIN (Quick Setup)
# ==========================================
@router.post("/kotak-totp-login")
async def connect_kotak_quick(req: KotakTotpOnly, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        user_data = await users_col.find_one({"id": current_user["id"]})

        if not user_data or user_data.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker profile not found.")

        client = NeoAPI(consumer_key=user_data.get("kotak_consumer_key"), environment='prod')
        
        # Login attempts
        client.totp_login(mobile_number=user_data.get("kotak_mobile"), ucc=user_data.get("kotak_ucc"), totp=req.totp)
        client.totp_validate(mpin=user_data.get("kotak_mpin"))

        # 🟢 STRICT CHECK: Kya sach mein login hua?
        verify_login_success(client)

        # Agar check pass ho gaya, toh memory mein save karo!
        KOTAK_SESSIONS[current_user["id"]] = client

        return {"status": "success", "message": "✅ Live Trading Session Started!"}
    
    except Exception as e:
        print(f"❌ Quick Kotak Connection Failed: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
