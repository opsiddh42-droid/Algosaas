import os
import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.api.v1.market import get_kotak_client
from bson import ObjectId

router = APIRouter()

class OrderModel(BaseModel):
    trading_symbol: str
    transaction_type: str
    quantity: int
    price: float
    order_type: str = "L"
    product: str = "NRML"
    token: str
    exchange_segment: str = "nse_fo"

@router.post("/place-order")
async def place_order(order: OrderModel, mode: str = "REAL", current_user: dict = Depends(get_current_user)):
    # Order logic as previously built, ensuring it saves correctly to real_trades
    client = get_kotak_client(current_user["id"])
    if mode == "REAL":
        try:
            client.place_order(
                exchange_segment=order.exchange_segment, product=order.product, price=str(order.price),
                order_type=order.order_type, quantity=str(order.quantity), validity="DAY",
                trading_symbol=order.trading_symbol, transaction_type=order.transaction_type, amo="NO"
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
            
    # Save Trade for UI
    from datetime import datetime
    db_col = get_collection("real_trades")
    leg = {
        "symbol": order.trading_symbol, "token": order.token, "transaction": order.transaction_type,
        "qty": order.quantity, "entry_price": order.price, "ltp": order.price, 
        "exch_seg": order.exchange_segment, "status": "OPEN"
    }
    await db_col.insert_one({
        "user_id": current_user["id"], "status": "OPEN", "is_algo": False,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "legs": [leg]
    })
    return {"status": "success", "message": "Order Executed"}

# 🟢 CUSTOM P&L & POSITIONS LOGIC 🟢
@router.get("/open-positions")
async def get_open_positions(mode: str = "REAL", current_user: dict = Depends(get_current_user)):
    db_col = get_collection("real_trades")
    open_trades = await db_col.find({"user_id": current_user["id"], "status": "OPEN"}).to_list(length=None)
    
    if not open_trades:
        return {"status": "success", "positions": []}

    try:
        client = get_kotak_client(current_user["id"])
    except:
        client = None

    formatted_positions = []
    
    for trade in open_trades:
        for leg in trade.get("legs", []):
            if leg.get("status") != "OPEN": continue
            
            ltp = leg.get("entry_price", 0)
            
            # 🟢 Fetch Live LTP if Kotak Client is active 🟢
            if client:
                try:
                    q = client.quotes(instrument_tokens=[{"instrument_token": str(leg["token"]), "exchange_segment": leg.get("exch_seg", "nse_fo")}], quote_type="all")
                    raw = q if isinstance(q, list) else q.get('data', [])
                    live_data = next((item for item in raw if str(item.get('exchange_token') or item.get('tk')) == str(leg["token"])), None)
                    if live_data and float(live_data.get('ltp', 0)) > 0:
                        ltp = float(live_data.get('ltp'))
                except:
                    pass
            
            # P&L Calculation: If we SOLD, profit is when LTP goes down. If BOUGHT, profit is when LTP goes up.
            entry_price = float(leg.get("entry_price", 0))
            qty = int(leg.get("qty", 0))
            if leg.get("transaction") == "S":
                mtm = (entry_price - ltp) * qty
            else:
                mtm = (ltp - entry_price) * qty
                
            formatted_positions.append({
                "id": str(trade["_id"]),
                "symbol": leg["symbol"],
                "transaction": leg["transaction"],
                "qty": qty,
                "entry_price": entry_price,
                "ltp": round(ltp, 2),
                "mtm": round(mtm, 2)
            })

    return {"status": "success", "positions": formatted_positions}

# 🟢 PANIC EXIT ALL (WITH "BUY" FIRST RULE PRIORITY) 🟢
@router.post("/exit-all")
async def exit_all_positions(mode: str = "REAL", current_user: dict = Depends(get_current_user)):
    db_col = get_collection("real_trades")
    open_trades = await db_col.find({"user_id": current_user["id"], "status": "OPEN"}).to_list(length=None)
    
    if not open_trades:
        return {"status": "success", "message": "No open positions to exit."}

    client = get_kotak_client(current_user["id"])
    
    all_open_legs = []
    for trade in open_trades:
        for leg in trade.get("legs", []):
            if leg.get("status") == "OPEN":
                all_open_legs.append({"trade_id": trade["_id"], "leg": leg})

    # 🚨 RULE ENFORCEMENT: Place exit order for 'Buy' positions before 'Sell' positions
    # If the original leg was "B" (Buy), we must exit it first.
    all_open_legs.sort(key=lambda x: 0 if x["leg"].get("transaction") == "B" else 1)

    for item in all_open_legs:
        trade_id = item["trade_id"]
        leg = item["leg"]
        
        exit_trans = "B" if leg.get("transaction") == "S" else "S"
        
        try:
            client.place_order(
                exchange_segment=leg.get("exch_seg", "nse_fo"), product="NRML", price="0", 
                order_type="MKT", quantity=str(leg["qty"]), validity="DAY", 
                trading_symbol=leg["symbol"], transaction_type=exit_trans, amo="NO"
            )
        except Exception as e:
            print(f"Panic Exit Error on {leg['symbol']}: {e}")
            
    # Mark all trades as CLOSED in DB
    await db_col.update_many(
        {"user_id": current_user["id"], "status": "OPEN"}, 
        {"$set": {"status": "CLOSED"}}
    )
    
    return {"status": "success", "message": "All positions squared off successfully."}

class CloseTradeRequest(BaseModel):
    trade_id: str
    mode: str = "REAL"

@router.post("/close-position")
async def close_single_position(req: CloseTradeRequest, current_user: dict = Depends(get_current_user)):
    db_col = get_collection("real_trades")
    trade = await db_col.find_one({"_id": ObjectId(req.trade_id), "user_id": current_user["id"]})
    
    if not trade or trade["status"] != "OPEN":
        raise HTTPException(status_code=400, detail="Trade not found or already closed.")
        
    client = get_kotak_client(current_user["id"])
    
    updated_legs = []
    for leg in trade.get("legs", []):
        if leg["status"] == "OPEN":
            exit_trans = "B" if leg["transaction"] == "S" else "S"
            try:
                client.place_order(
                    exchange_segment=leg.get("exch_seg", "nse_fo"), product="NRML", price="0", 
                    order_type="MKT", quantity=str(leg["qty"]), validity="DAY", 
                    trading_symbol=leg["symbol"], transaction_type=exit_trans, amo="NO"
                )
                leg["status"] = "CLOSED"
            except Exception as e:
                pass
        updated_legs.append(leg)

    await db_col.update_one({"_id": ObjectId(req.trade_id)}, {"$set": {"status": "CLOSED", "legs": updated_legs}})
    return {"status": "success"}
