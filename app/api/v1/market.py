from fastapi import APIRouter, HTTPException, Depends
from app.api.deps import get_current_user
from app.services.kotak_api import generate_option_chain

router = APIRouter()

@router.get("/option-chain/{index_name}")
async def get_option_chain(
    index_name: str, 
    current_ltp: float, 
    current_user: dict = Depends(get_current_user)
):
    """
    SaaS Portal ke liye Live Option Chain Structure.
    """
    try:
        # Pata lagate hain option chain (default 10 strikes up/down)
        result = await generate_option_chain(index_name=index_name.upper(), current_ltp=current_ltp, range_strikes=10)
        
        if result["status"] == "error":
            raise HTTPException(status_code=400, detail=result["message"])
            
        return result
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
