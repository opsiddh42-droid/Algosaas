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
            
    # Save Trade for UI (History ke liye DB mein save rakha hai)
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

# 🟢 DIRECT KOTAK LIVE P&L & POSITIONS LOGIC (NO MONGODB LAFDA) 🟢
@router.get("/open-positions")
async def get_open_positions(mode: str = "REAL", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
    except:
        return {"status": "success", "positions": []}

    try:
        # Seedha Broker se positions mango
        pos_response = client.positions()
        pos_data = pos_response if isinstance(pos_response, list) else pos_response.get("data", [])
        
        active_positions = []
        tokens_to_fetch = []
        
        for p in pos_data:
            # Kotak gives buy and sell quantities, calculate Net Qty
            buy_qty = int(p.get("flBuyQty", p.get("buyQty", 0)))
            sell_qty = int(p.get("flSellQty", p.get("sellQty", 0)))
            net_qty = buy_qty - sell_qty
            
            # Agar Net Qty Zero nahi hai, toh position sach mein open hai
            if net_qty != 0:
                tok = str(p.get("tok", ""))
                ex_seg = p.get("exSeg", "nse_fo")
                if tok: 
                    tokens_to_fetch.append({"instrument_token": tok, "exchange_segment": ex_seg})
                
                buy_amt = float(p.get("buyAmt", 0))
                sell_amt = float(p.get("sellAmt", 0))
                
                transaction = "B" if net_qty > 0 else "S"
                abs_qty = abs(net_qty)
                
                if net_qty > 0:
                    avg_price = buy_amt / buy_qty if buy_qty > 0 else 0
                else:
                    avg_price = sell_amt / sell_qty if sell_qty > 0 else 0
                    
                active_positions.append({
                    "id": tok, # UI mein square off ke liye token bheja hai
                    "symbol": p.get("trdSym"),
                    "token": tok,
                    "transaction": transaction,
                    "qty": abs_qty,
                    "entry_price": avg_price,
                    "ltp": avg_price, 
                    "mtm": 0.0,
                    "exch_seg": ex_seg
                })

        # Fetch Live LTP for calculating MTM
        if tokens_to_fetch and active_positions:
            try:
                q = client.quotes(instrument_tokens=tokens_to_fetch, quote_type="ltp")
                raw_quotes = q if isinstance(q, list) else q.get('data', [])
                ltp_dict = {str(item.get('exchange_token', item.get('tk'))): float(item.get('ltp', 0)) for item in raw_quotes}
                
                for pos in active_positions:
                    ltp = ltp_dict.get(pos["token"], pos["entry_price"])
                    pos["ltp"] = round(ltp, 2)
                    
                    if pos["transaction"] == "B":
                        pos["mtm"] = round((ltp - pos["entry_price"]) * pos["qty"], 2)
                    else:
                        pos["mtm"] = round((pos["entry_price"] - ltp) * pos["qty"], 2)
            except Exception as e:
                pass
                
        return {"status": "success", "positions": active_positions}
    except Exception as e:
        return {"status": "success", "positions": []}


# 🟢 PANIC EXIT ALL (KOTAK LIVE FETCH PRIORITY) 🟢
@router.post("/exit-all")
async def exit_all_positions(mode: str = "REAL", current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
    except:
        return {"status": "error", "message": "Kotak not connected."}
        
    pos_response = client.positions()
    pos_data = pos_response if isinstance(pos_response, list) else pos_response.get("data", [])
    
    open_legs = []
    for p in pos_data:
        buy_qty = int(p.get("flBuyQty", p.get("buyQty", 0)))
        sell_qty = int(p.get("flSellQty", p.get("sellQty", 0)))
        net_qty = buy_qty - sell_qty
        
        if net_qty != 0:
            open_legs.append({
                "symbol": p.get("trdSym"),
                "qty": abs(net_qty),
                "transaction": "B" if net_qty > 0 else "S",
                "exch_seg": p.get("exSeg", "nse_fo")
            })

    if not open_legs:
        return {"status": "success", "message": "No open positions to exit."}

    # 🚨 RULE ENFORCEMENT: Place exit order for 'Buy' positions before 'Sell' positions
    open_legs.sort(key=lambda x: 0 if x["transaction"] == "B" else 1)

    for leg in open_legs:
        # Reverse the transaction to square off
        exit_trans = "S" if leg["transaction"] == "B" else "B" 
        
        try:
            client.place_order(
                exchange_segment=leg.get("exch_seg", "nse_fo"), product="NRML", price="0", 
                order_type="MKT", quantity=str(leg["qty"]), validity="DAY", 
                trading_symbol=leg["symbol"], transaction_type=exit_trans, amo="NO"
            )
        except Exception as e:
            print(f"Panic Exit Error on {leg['symbol']}: {e}")
            
    # Mark all trades as CLOSED in DB just for cleanup
    db_col = get_collection("real_trades")
    await db_col.update_many(
        {"user_id": current_user["id"], "status": "OPEN"}, 
        {"$set": {"status": "CLOSED"}}
    )
    
    return {"status": "success", "message": "All positions squared off successfully."}

class CloseTradeRequest(BaseModel):
    trade_id: str # Ab yeh token aayega frontend se
    mode: str = "REAL"

# 🟢 SINGLE CLOSE POSITION (KOTAK LIVE) 🟢
@router.post("/close-position")
async def close_single_position(req: CloseTradeRequest, current_user: dict = Depends(get_current_user)):
    try:
        client = get_kotak_client(current_user["id"])
    except:
        raise HTTPException(status_code=400, detail="Kotak not connected.")
        
    pos_response = client.positions()
    pos_data = pos_response if isinstance(pos_response, list) else pos_response.get("data", [])
    
    for p in pos_data:
        tok = str(p.get("tok", ""))
        # Check if this is the token we want to close
        if tok == req.trade_id:
            buy_qty = int(p.get("flBuyQty", p.get("buyQty", 0)))
            sell_qty = int(p.get("flSellQty", p.get("sellQty", 0)))
            net_qty = buy_qty - sell_qty
            
            if net_qty != 0:
                exit_trans = "S" if net_qty > 0 else "B"
                try:
                    client.place_order(
                        exchange_segment=p.get("exSeg", "nse_fo"), product="NRML", price="0", 
                        order_type="MKT", quantity=str(abs(net_qty)), validity="DAY", 
                        trading_symbol=p.get("trdSym"), transaction_type=exit_trans, amo="NO"
                    )
                except Exception as e:
                    raise HTTPException(status_code=400, detail=str(e))
            break

    # Clean DB for this specific token so it doesn't mess up history
    db_col = get_collection("real_trades")
    await db_col.update_many(
        {"user_id": current_user["id"], "legs.token": req.trade_id}, 
        {"$set": {"status": "CLOSED", "legs.$[].status": "CLOSED"}}
    )
    
    return {"status": "success"}
