from fastapi import APIRouter, HTTPException, Depends
from typing import List
from app.models.strategy import StrategyConfig
from app.api.deps import get_current_user
from app.core.database import get_collection

router = APIRouter()

@router.post("/save")
async def save_strategy(
    strategy_data: StrategyConfig, 
    current_user: dict = Depends(get_current_user)
):
    """Nayi strategy save karne ke liye"""
    try:
        strat_col = get_collection("strategies")
        # Ensure user_id is set to current logged in user
        strategy_dict = strategy_data.dict()
        strategy_dict["user_id"] = current_user["id"]
        
        await strat_col.insert_one(strategy_dict)
        return {"status": "success", "message": f"Strategy '{strategy_data.strategy_name}' saved!"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/my-strategies", response_model=List[StrategyConfig])
async def get_user_strategies(current_user: dict = Depends(get_current_user)):
    """User ki saari strategies ki list mangwane ke liye"""
    strat_col = get_collection("strategies")
    cursor = strat_col.find({"user_id": current_user["id"]})
    strategies = await cursor.to_list(length=100)
    return strategies

@router.patch("/toggle/{strategy_id}")
async def toggle_strategy(strategy_id: str, is_active: bool, current_user: dict = Depends(get_current_user)):
    """Strategy ko On ya Off karne ke liye"""
    strat_col = get_collection("strategies")
    await strat_col.update_one(
        {"strategy_id": strategy_id, "user_id": current_user["id"]},
        {"$set": {"is_active": is_active}}
    )
    return {"status": "success", "message": "Strategy status updated"}
