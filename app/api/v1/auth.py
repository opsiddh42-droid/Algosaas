from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel, EmailStr
import uuid
from datetime import timedelta

from app.models.user import UserCreate
from app.api.deps import get_current_user
from app.core.security import get_password_hash, verify_password, create_access_token
from app.core.database import get_collection
from app.core.config import settings

# 🟢 IN-MEMORY SESSIONS IMPORT KIYA "SMART STATUS" KE LIYE
from app.core.sessions import KOTAK_SESSIONS

router = APIRouter()

# 🚀 OPTIMIZATION: EmailStr use kiya validation ke liye
class LoginRequest(BaseModel):
    email: EmailStr
    password: str

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate):
    users_col = get_collection("users")
    
    # 🚀 OPTIMIZATION: Single Query mein Email aur Mobile check
    existing_user = await users_col.find_one({
        "$or": [
            {"email": user.email},
            {"mobile": user.mobile}
        ]
    })
    
    if existing_user:
        if existing_user.get("email") == user.email:
            raise HTTPException(status_code=400, detail="Email is already registered")
        else:
            raise HTTPException(status_code=400, detail="Mobile number is already registered")

    # Secure data tayar karna
    user_id = str(uuid.uuid4())
    hashed_pass = get_password_hash(user.password)
    
    new_user_dict = {
        "id": user_id,
        "full_name": user.full_name,
        "email": user.email,
        "hashed_password": hashed_pass,
        "mobile": user.mobile,
        "is_active": True,
        "kotak_status": "Inactive"
    }
    
    # MongoDB mein save karna
    await users_col.insert_one(new_user_dict)
    
    return {"status": "success", "message": "Account created successfully! Please login."}


@router.post("/login")
async def login(credentials: LoginRequest):
    users_col = get_collection("users")
    
    # User ko email se dhoondhna
    user = await users_col.find_one({"email": credentials.email})
    
    # 🚀 SECURITY: Combined error (Hacker guess nahi kar payega kya galat tha)
    if not user or not verify_password(credentials.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid Email or Password")
        
    # Agar sab theek hai, toh JWT Token generate karna
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["id"]}, expires_delta=access_token_expires
    )
    
    # 🚀 SMART STATUS LOGIC (Daily TOTP Check)
    actual_kotak_status = user.get("kotak_status", "Inactive")
    
    if actual_kotak_status == "Active":
        # Agar DB mein Active hai par RAM mein session zinda nahi hai (Naya din/Restart)
        if user["id"] not in KOTAK_SESSIONS:
            actual_kotak_status = "NeedsTOTP"
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_name": user["full_name"],
        "kotak_status": actual_kotak_status  # Updated status bheja
    }


# 🚀 NEW FEATURE: Permanently Delete Account & Data
@router.delete("/delete-account")
async def delete_account(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    
    users_col = get_collection("users")
    algo_col = get_collection("algo_state")
    trades_col = get_collection("real_trades")
    
    # 1. User ki Algo Settings delete karna
    await algo_col.delete_one({"user_id": user_id})
    
    # 2. User ki Trading History delete karna
    await trades_col.delete_many({"user_id": user_id})
    
    # 3. User ki Main Profile delete karna
    delete_result = await users_col.delete_one({"id": user_id})
    
    # Memory se bhi session clear kar do agar hai toh (Clean Logout)
    if user_id in KOTAK_SESSIONS:
        KOTAK_SESSIONS.pop(user_id, None)
    
    if delete_result.deleted_count == 0:
        raise HTTPException(status_code=400, detail="Account not found or already deleted.")
    
    return {"status": "success", "message": "Your account and all associated data have been permanently deleted."}
