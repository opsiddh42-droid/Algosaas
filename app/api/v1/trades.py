from fastapi import APIRouter, HTTPException, Depends
from app.api.deps import get_current_user
from app.engine.risk_manager import execute_safe_exit_all
from neo_api_client import NeoAPI

router = APIRouter()

# Note: Ek real SaaS mein humein user ka active Kotak session cache/Redis mein 
# maintain karna hota hai. Yahan testing ke liye hum ek dummy function maan rahe hain 
# jo session return karta hai.
def get_active_kotak_session(user):
    # Agar user ke paas consumer_key nahi hai toh error throw karein
    if not user.get("kotak_consumer_key"):
        raise HTTPException(status_code=400, detail="Kotak account not linked.")
    
    # Session initialize karein (Real system me yahan Redis se live session fetch hoga)
    client = NeoAPI(consumer_key=user["kotak_consumer_key"], environment='prod')
    # Assuming token validation is maintained elsewhere in a background task
    return client

@router.post("/exit-all")
async def trigger_exit_all(current_user: dict = Depends(get_current_user)):
    try:
        # 1. User ka active Kotak session fetch karein
        kotak_client = get_active_kotak_session(current_user)
        
        # 2. Safe Exit algorithm ko call karein
        result = await execute_safe_exit_all(user_id=current_user["id"], kotak_client=kotak_client)
        
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Exit All Failed: {str(e)}")

@router.get("/positions")
async def get_open_positions(current_user: dict = Depends(get_current_user)):
    # Yahan DB se ya Kotak API se user ki live positions fetch karke return karenge
    return {"status": "success", "positions": []}
