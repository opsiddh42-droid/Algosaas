from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
import uuid

# Ek single trade leg ka structure
class StrategyLeg(BaseModel):
    option_type: str       # "CE" ya "PE"
    side: str              # "BUY" ya "SELL"
    strike_offset: int     # e.g., 0 (ATM), 100 (OTM), -100 (ITM)
    lots: int
    target_profit: Optional[float] = None
    stop_loss: Optional[float] = None

# Poori strategy ka structure (Ek user aisi multiple strategies bana sakega)
class StrategyConfig(BaseModel):
    # Har strategy ko ek unique ID milegi taaki update/delete aasan ho
    strategy_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str           # Kis user ki strategy hai
    strategy_name: str     # Ex: "BankNifty 9:20 Straddle"
    index: str             # "NIFTY", "BANKNIFTY", "SENSEX"
    entry_time: str        # Ex: "09:20"
    exit_time: str         # Ex: "15:15"
    legs: List[StrategyLeg]
    
    # User kisi bhi strategy ko portal se on/off kar sakega
    is_active: bool = True 
    created_at: datetime = datetime.utcnow()
