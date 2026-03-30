from pydantic import BaseModel, EmailStr, Field
from typing import Optional
from datetime import datetime

# 1. User Registration ke time aane wala data
class UserCreate(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=50)
    email: EmailStr
    password: str = Field(..., min_length=6)
    mobile: str = Field(..., min_length=10, max_length=15)

# 2. Database mein save hone wala complete data structure
class UserDB(BaseModel):
    id: str
    full_name: str
    email: EmailStr
    hashed_password: str
    mobile: str
    is_active: bool = True
    created_at: datetime = datetime.utcnow()
    
    # Broker details (Kotak Neo) initially empty rahenge
    kotak_ucc: Optional[str] = None
    kotak_consumer_key: Optional[str] = None
    kotak_status: str = "Inactive"
