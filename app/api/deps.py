from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from app.core.config import settings
from app.core.database import get_collection

# 🟢 IN-MEMORY SESSIONS IMPORT KIYA "SMART STATUS" KE LIYE
from app.core.sessions import KOTAK_SESSIONS

# Yeh FastAPI ko batayega ki token kahan se uthana hai
oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        # Token ko decode karna
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        user_id: str = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
        
    # Database se user fetch karna
    users_col = get_collection("users")
    user = await users_col.find_one({"id": user_id})
    if user is None:
        raise credentials_exception
        
    # 🚀 SMART STATUS LOGIC YAHAN BHI LAGEGA
    # Taaki agar refresh ho ya internal call ho, toh live memory check ho
    actual_kotak_status = user.get("kotak_status", "Inactive")
    if actual_kotak_status == "Active":
        if user["id"] not in KOTAK_SESSIONS:
            user["kotak_status"] = "NeedsTOTP"  # DB mein Active hai par RAM mein nahi
            
    # Agar user ko block ya ban karna ho future mein uski security:
    if not user.get("is_active", True):
        raise HTTPException(status_code=400, detail="Inactive user account")
        
    return user
