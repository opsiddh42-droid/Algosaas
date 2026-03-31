from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List
from datetime import datetime
import uuid

from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

# --- 1. PYDANTIC MODELS (Frontend se aane wale data ka structure) ---
class LegModel(BaseModel):
    id: int
    segment: str
    lot: str
    position: str
    optType: str
    expiry: str
    criteria: str
    strikeType: str

class StrategyModel(BaseModel):
    index: str
    underlying: str
    type: str
    entryTime: str
    exitTime: str
    legs: List[LegModel]

# ==========================================
# ROUTE 1: SAVE NEW STRATEGY
# ==========================================
@router.post("/save")
async def save_strategy(strategy: StrategyModel, current_user: dict = Depends(get_current_user)):
    try:
        strat_col = get_collection("strategies")
        
        # Strategy ke liye ek unique ID aur status generate karo
        strategy_data = strategy.dict()
        strategy_data["strategy_id"] = str(uuid.uuid4())
        strategy_data["user_id"] = current_user["id"]
        strategy_data["is_active"] = True  # Default chalu rahegi
        strategy_data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Database mein save kar do
        await strat_col.insert_one(strategy_data)

        return {
            "status": "success", 
            "message": "✅ Strategy Saved Successfully!", 
            "strategy_id": strategy_data["strategy_id"]
        }
    except Exception as e:
        print(f"❌ Strategy Save Failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to save strategy: {str(e)}")


# ==========================================
# ROUTE 2: GET ALL SAVED STRATEGIES
# ==========================================
@router.get("/list")
async def list_strategies(current_user: dict = Depends(get_current_user)):
    try:
        strat_col = get_collection("strategies")
        
        # User ki saari strategies nikalo
        cursor = strat_col.find({"user_id": current_user["id"]})
        strategies = await cursor.to_list(length=100)
        
        # MongoDB ka default '_id' JSON mein convert nahi hota, isliye usey hata do
        for strat in strategies:
            strat["_id"] = str(strat["_id"])

        return {"status": "success", "data": strategies}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch strategies: {str(e)}")


# ==========================================
# ROUTE 3: DELETE STRATEGY
# ==========================================
@router.delete("/delete/{strategy_id}")
async def delete_strategy(strategy_id: str, current_user: dict = Depends(get_current_user)):
    try:
        strat_col = get_collection("strategies")
        result = await strat_col.delete_one({"strategy_id": strategy_id, "user_id": current_user["id"]})
        
        if result.deleted_count == 0:
            raise HTTPException(status_code=404, detail="Strategy not found")
            
        return {"status": "success", "message": "🗑️ Strategy Deleted Successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete: {str(e)}")
