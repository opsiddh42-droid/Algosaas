from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.api.deps import get_current_user
from app.core.database import get_collection
from app.core.sessions import KOTAK_SESSIONS
from bson import ObjectId
from datetime import datetime
import time

router = APIRouter()

# --- 1. MODELS (Added Stoploss & Target) ---
class OrderModel(BaseModel):
    exchange_segment: str = "nse_fo"
    trading_symbol: str
    transaction_type: str  
    quantity: str
    order_type: str = "L"  
    price: str             
    product: str = "NRML"
    token: str = "" 
    stoploss: str = "0"  # 🟢 Naya Field
    target: str = "0"    # 🟢 Naya Field

class ClosePosRequest(BaseModel):
    trade_id: str
    mode: str = "PAPER"

def get_kotak_client(user_id: str):
    if user_id not in KOTAK_SESSIONS:
        raise HTTPException(status_code=401, detail="Kotak Session is OFF!")
    return KOTAK_SESSIONS[user_id]

# ==========================================
# 🟢 ROUTE 1: PLACE ORDER (PAPER + REAL)
# ==========================================
@router.post("/place-order")
async def place_trade(order: OrderModel, mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    try:
        if mode == "PAPER":
            # 📝 PAPER TRADE LOGIC (Saves SL and Target)
            paper_col = get_collection("paper_trades")
            
            trade_doc = {
                "user_id": current_user["id"],
                "status": "OPEN",
                "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "legs": [
                    {
                        "symbol": order.trading_symbol,
                        "token": order.token,
                        "transaction": order.transaction_type,
                        "qty": int(order.quantity),
                        "entry_price": float(order.price),
                        "ltp": float(order.price),
                        "sl": float(order.stoploss),     # 🟢 DB mein SL save
                        "target": float(order.target),   # 🟢 DB mein Target save
                        "status": "OPEN"
                    }
                ]
            }
            
            await paper_col.insert_one(trade_doc)
            return {"status": "success", "message": f"Paper Order Saved! SL: {order.stoploss}, TGT: {order.target}"}

        else:
            # 💸 REAL TRADE LOGIC
            users_col = get_collection("users")
            db_user = await users_col.find_one({"id": current_user["id"]})
            if not db_user or db_user.get("kotak_status") != "Active":
                raise HTTPException(status_code=400, detail="Broker profile not connected.")

            client = get_kotak_client(current_user["id"])
            
            # Real Kotak API call
            resp = client.place_order(
                exchange_segment=order.exchange_segment,
                product=order.product,
                price=str(order.price),
                order_type="L",  
                quantity=order.quantity,
                validity="DAY",
                trading_symbol=order.trading_symbol,
                transaction_type=order.transaction_type,
                amo="NO"
            )

            if isinstance(resp, dict) and 'nOrdNo' in resp:
                return {"status": "success", "message": f"Real Order Placed! ID: {resp['nOrdNo']}"}
            else:
                raise Exception(str(resp))

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Order Failed: {str(e)}")

# ==========================================
# ROUTE 2: PANIC EXIT ALL
# ==========================================
@router.post("/exit-all")
async def panic_exit_all(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    try:
        if mode == "PAPER":
            paper_col = get_collection("paper_trades")
            result = await paper_col.update_many(
                {"user_id": current_user["id"], "status": "OPEN"},
                {"$set": {"status": "CLOSED", "exit_time": datetime.now().strftime("%H:%M")}}
            )
            return {"status": "success", "message": f"🚨 Paper Exit Complete!"}
        else:
            client = get_kotak_client(current_user["id"])
            positions_response = client.positions()
            
            if not isinstance(positions_response, dict) or 'data' not in positions_response:
                return {"status": "success", "message": "No open positions found."}

            open_positions = positions_response['data']
            buy_exit_orders = []   
            sell_exit_orders = []  

            for pos in open_positions:
                net_qty = int(pos.get('netTrdQty', pos.get('flldQty', 0)))
                if net_qty == 0: continue 
                    
                sym = pos.get('trdSym')
                exch = pos.get('exSeg')
                prod = pos.get('prd')
                token = pos.get('tok')

                ltp = 0.0
                try:
                    if token:
                        q = client.quotes(instrument_tokens=[{"instrument_token": str(token), "exchange_segment": exch}], quote_type="all")
                        if isinstance(q, list) and len(q) > 0: ltp = float(q[0].get('ltp', 0))
                        elif isinstance(q, dict) and 'data' in q and len(q['data']) > 0: ltp = float(q['data'][0].get('ltp', 0))
                except: pass

                if ltp == 0: ltp = float(pos.get('buyAmt', 0)) 

                if net_qty > 0:
                    sell_exit_orders.append({"exchange_segment": exch, "product": prod, "price": str(round(ltp * 0.85, 1)), "order_type": "L", "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym, "transaction_type": "S", "amo": "NO"})
                elif net_qty < 0:
                    buy_exit_orders.append({"exchange_segment": exch, "product": prod, "price": str(round(ltp * 1.15, 1)), "order_type": "L", "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym, "transaction_type": "B", "amo": "NO"})

            exit_count = 0
            for order in buy_exit_orders:
                client.place_order(**order)
                exit_count += 1
                time.sleep(0.2) 
            for order in sell_exit_orders:
                client.place_order(**order)
                exit_count += 1
                time.sleep(0.2)

            return {"status": "success", "message": f"🚨 Real Exit Complete! {exit_count} positions closed."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ROUTE 3: GET OPEN POSITIONS 
# ==========================================
@router.get("/open-positions")
async def get_open_positions(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    try:
        positions_data = []
        if mode == "PAPER":
            paper_col = get_collection("paper_trades")
            open_trades = await paper_col.find({"user_id": current_user["id"], "status": "OPEN"}).to_list(length=100)
            
            for trade in open_trades:
                for leg in trade.get("legs", []):
                    if leg.get("status") == "OPEN":
                        positions_data.append({
                            "id": str(trade["_id"]),
                            "symbol": leg["symbol"],
                            "token": leg["token"],
                            "transaction": leg["transaction"],
                            "qty": leg["qty"],
                            "entry_price": leg["entry_price"],
                            "sl": leg.get("sl", 0),          # Frontend ko bhej rahe hain
                            "target": leg.get("target", 0)   # Frontend ko bhej rahe hain
                        })
        else:
            users_col = get_collection("users")
            db_user = await users_col.find_one({"id": current_user["id"]})
            if db_user and db_user.get("kotak_status") == "Active":
                client = get_kotak_client(current_user["id"])
                pos_resp = client.positions()
                if isinstance(pos_resp, dict) and 'data' in pos_resp:
                    for p in pos_resp['data']:
                        net_qty = int(p.get('netTrdQty', p.get('flldQty', 0)))
                        if net_qty != 0:
                            positions_data.append({
                                "id": str(p.get('tok')), 
                                "symbol": p.get('trdSym'),
                                "token": p.get('tok'),
                                "transaction": "B" if net_qty > 0 else "S",
                                "qty": abs(net_qty),
                                "entry_price": float(p.get('buyAmt', 0)) / abs(net_qty) if net_qty > 0 else float(p.get('sellAmt', 0)) / abs(net_qty)
                            })
        return {"status": "success", "positions": positions_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# ROUTE 4: SINGLE SQUARE OFF
# ==========================================
@router.post("/close-position")
async def close_position(req: ClosePosRequest, current_user: dict = Depends(get_current_user)):
    try:
        if req.mode == "PAPER":
            paper_col = get_collection("paper_trades")
            await paper_col.update_one(
                {"_id": ObjectId(req.trade_id)}, 
                {"$set": {"status": "CLOSED", "exit_time": datetime.now().strftime("%H:%M")}}
            )
            return {"status": "success", "message": "Paper Position Closed"}
        else:
            return {"status": "success", "message": "Real Position Square Off Logic Pending"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
