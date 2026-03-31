from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
import uuid

from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

# --- 1. PYDANTIC MODELS (Updated for Pro Features & Paper Trade) ---

# Chote models jo baki jagah use honge (SL, Target ke liye)
class ConditionModel(BaseModel):
    enabled: bool
    type: Optional[str] = None
    value: Optional[str] = None

class SpotTriggerModel(BaseModel):
    enabled: bool
    direction: Optional[str] = None
    points: Optional[str] = None

class OverallSLModel(BaseModel):
    enabled: bool
    value: Optional[str] = None

class LegModel(BaseModel):
    id: int
    segment: str
    lot: str
    position: str
    optType: str
    expiry: str
    criteria: str
    targetValue: str        # Pehle yeh sirf strikeType tha
    target: ConditionModel  # Har leg ka apna target
    sl: ConditionModel      # Har leg ka apna SL

class StrategyModel(BaseModel):
    executionMode: str = "PAPER"  # 🟢 NAYA: Paper ya Real mode record karega
    index: str
    underlying: str
    type: str               # Intraday, BTST, Positional
    entryTime: str
    exitTime: str
    exitDate: Optional[str] = None 
    spotTrigger: SpotTriggerModel
    overallSL: OverallSLModel
    legs: List[LegModel]


# ==========================================
# ROUTE 1: SAVE NEW STRATEGY
# ==========================================
@router.post("/save")
async def save_strategy(strategy: StrategyModel, current_user: dict = Depends(get_current_user)):
    try:
        strat_col = get_collection("strategies")
        
        # Strategy ke liye ek unique ID aur default status generate karo
        strategy_data = strategy.dict()
        strategy_data["strategy_id"] = str(uuid.uuid4())
        strategy_data["user_id"] = current_user["id"]
        strategy_data["is_active"] = True  
        strategy_data["status"] = "WAITING" # 🟢 Engine ko batane ke liye ki abhi entry nahi hui
        strategy_data["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Database mein save kar do
        await strat_col.insert_one(strategy_data)

        # Dynamic Message (Paper ya Real)
        mode_text = strategy_data.get("executionMode", "PAPER")

        return {
            "status": "success", 
            "message": f"✅ {mode_text} Strategy Saved Successfully!", 
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
