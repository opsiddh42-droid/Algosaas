from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

# Frontend se aane wale Kotak details ka structure
class KotakCredentials(BaseModel):
    mobile: str
    ucc: str
    mpin: str
    totp: str
    consumer_key: str

@router.post("/kotak-login")
async def connect_kotak(
    creds: KotakCredentials, 
    current_user: dict = Depends(get_current_user)  # Yeh ensure karega ki user logged in hai
):
    try:
        print(f"🔄 Connecting Kotak Neo for User: {current_user['full_name']} (UCC: {creds.ucc})")
        
        # 1. Kotak Neo Client Initialize karna
        client = NeoAPI(consumer_key=creds.consumer_key, environment='prod')
        
        # 2. TOTP Login
        client.totp_login(mobile_number=creds.mobile, ucc=creds.ucc, totp=creds.totp)
        
        # 3. MPIN Validate (Final Step)
        client.totp_validate(mpin=creds.mpin)

        # 4. Agar login successful raha, toh Database mein user ka broker status update karna
        users_col = get_collection("users")
        await users_col.update_one(
            {"id": current_user["id"]},
            {
                "$set": {
                    "kotak_status": "Active",
                    "kotak_ucc": creds.ucc,
                    "kotak_consumer_key": creds.consumer_key,
                    "kotak_mobile": creds.mobile
                    # Note: MPIN aur TOTP database mein save nahi karenge for security
                }
            }
        )

        return {
            "status": "success", 
            "message": "✅ Kotak Neo Connected Successfully!",
            "ucc": creds.ucc
        }
    
    except Exception as e:
        print(f"❌ Kotak Connection Failed: {str(e)}")
        # Agar error aaya (jaise galat TOTP ya MPIN), toh frontend ko error bhejenge
        raise HTTPException(status_code=400, detail=f"Broker Login Failed: {str(e)}")
