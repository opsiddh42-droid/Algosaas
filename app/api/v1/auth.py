from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
import uuid
from datetime import timedelta

from app.models.user import UserCreate
from app.core.security import get_password_hash, verify_password, create_access_token
from app.core.database import get_collection
from app.core.config import settings

router = APIRouter()

# Login request ke liye chota sa model
class LoginRequest(BaseModel):
    email: str
    password: str

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def create_user(user: UserCreate):
    users_col = get_collection("users")
    
    # Check karein ki user pehle se toh nahi hai
    existing_user = await users_col.find_one({"email": user.email})
    if existing_user:
        raise HTTPException(status_code=400, detail="Email already registered")
        
    existing_mobile = await users_col.find_one({"mobile": user.mobile})
    if existing_mobile:
        raise HTTPException(status_code=400, detail="Mobile number already registered")

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
    
    return {"status": "success", "message": "User created successfully! Please login."}


@router.post("/login")
async def login(credentials: LoginRequest):
    users_col = get_collection("users")
    
    # User ko email se dhoondhna
    user = await users_col.find_one({"email": credentials.email})
    if not user:
        raise HTTPException(status_code=401, detail="Invalid Email or Password")
        
    # Password match karna
    if not verify_password(credentials.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Invalid Email or Password")
        
    # Agar sab theek hai, toh JWT Token generate karna
    access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user["id"]}, expires_delta=access_token_expires
    )
    
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user_name": user["full_name"],
        "kotak_status": user["kotak_status"]
    }
