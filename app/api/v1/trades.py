from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
from bson import ObjectId
from datetime import datetime
import time

router = APIRouter()

# --- 1. MODELS ---
class OrderModel(BaseModel):
    exchange_segment: str = "nse_fo"
    trading_symbol: str
    transaction_type: str  # "B" for Buy, "S" for Sell
    quantity: str
    order_type: str = "L"  # ✅ Ab default "L" (Limit) rahega
    price: str             # ✅ Price dena mandatory hai
    product: str = "NRML"

class ClosePosRequest(BaseModel):
    trade_id: str
    mode: str = "PAPER"

# --- 2. HELPER: GET KOTAK CLIENT ---
def get_kotak_client(db_user: dict):
    try:
        client = NeoAPI(
            consumer_key=db_user["kotak_consumer_key"], 
            environment='prod'
        )
        return client
    except Exception as e:
        raise HTTPException(status_code=401, detail="Failed to initialize Broker Client. Session might be expired.")


# ==========================================
# ROUTE 1: PLACE SINGLE ORDER (LIMIT)
# ==========================================
@router.post("/place-order")
async def place_trade(order: OrderModel, current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        db_user = await users_col.find_one({"id": current_user["id"]})

        if not db_user or db_user.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker profile not connected.")

        client = get_kotak_client(db_user)

        print(f"🚀 Placing LIMIT Order: {order.transaction_type} {order.quantity} {order.trading_symbol} @ {order.price}")

        # Kotak API - Limit Order
        resp = client.place_order(
            exchange_segment=order.exchange_segment,
            product=order.product,
            price=str(order.price),
            order_type="L",  # Strictly Limit
            quantity=order.quantity,
            validity="DAY",
            trading_symbol=order.trading_symbol,
            transaction_type=order.transaction_type,
            amo="NO"
        )

        if isinstance(resp, dict) and 'nOrdNo' in resp:
            return {
                "status": "success", 
                "message": f"Limit Order Placed! ID: {resp['nOrdNo']}", 
                "order_id": resp['nOrdNo']
            }
        else:
            raise Exception(str(resp))

    except Exception as e:
        print(f"❌ Order Execution Failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Order Failed: {str(e)}")


# ==========================================
# ROUTE 2: PANIC EXIT ALL (PAPER + REAL SMART BUFFER)
# ==========================================
@router.post("/exit-all")
async def panic_exit_all(mode: str = "PAPER", current_user: dict = Depends(get_current_user)):
    try:
        if mode == "PAPER":
            # 📝 PAPER MODE EXIT ALL
            paper_col = get_collection("paper_trades")
            result = await paper_col.update_many(
                {"user_id": current_user["id"], "status": "OPEN"},
                {"$set": {"status": "CLOSED", "exit_time": datetime.now().strftime("%H:%M")}}
            )
            return {"status": "success", "message": f"🚨 Paper Panic Exit Complete! {result.modified_count} virtual positions squared off."}

        else:
            # 💸 REAL MODE EXIT ALL (User's Smart Buffer Limit Logic)
            users_col = get_collection("users")
            db_user = await users_col.find_one({"id": current_user["id"]})

            if not db_user or db_user.get("kotak_status") != "Active":
                raise HTTPException(status_code=400, detail="Broker profile not connected.")

            client = get_kotak_client(db_user)
            print("🚨 REAL PANIC EXIT INITIATED! Fetching open positions...")

            positions_response = client.positions()
            
            if not isinstance(positions_response, dict) or 'data' not in positions_response:
                return {"status": "success", "message": "No open positions found to exit."}

            open_positions = positions_response['data']
            buy_exit_orders = []   
            sell_exit_orders = []  

            for pos in open_positions:
                net_qty = int(pos.get('netTrdQty', pos.get('flldQty', 0)))
                if net_qty == 0: continue 
                    
                sym = pos.get('trdSym', pos.get('trading_symbol', ''))
                exch = pos.get('exSeg', pos.get('exchange_segment', 'nse_fo'))
                prod = pos.get('prd', pos.get('product', 'NRML'))
                token = pos.get('tok', pos.get('instrument_token', ''))

                ltp = 0.0
                try:
                    if token:
                        q = client.quotes(instrument_tokens=[{"instrument_token": str(token), "exchange_segment": exch}], quote_type="all")
                        quote_data = q.get('data', []) if isinstance(q, dict) else q
                        ltp = float(quote_data[0].get('ltp', quote_data[0].get('lastPrice', 0)))
                except:
                    pass

                if ltp == 0:
                    ltp = float(pos.get('buyAmt', 0)) 

                if net_qty > 0:
                    # SELL (15% Niche)
                    safe_limit_price = round(ltp * 0.85, 1) 
                    sell_exit_orders.append({
                        "exchange_segment": exch, "product": prod, "price": str(safe_limit_price), "order_type": "L",
                        "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym,
                        "transaction_type": "S", "amo": "NO"
                    })
                elif net_qty < 0:
                    # BUY (15% Upar)
                    safe_limit_price = round(ltp * 1.15, 1) 
                    buy_exit_orders.append({
                        "exchange_segment": exch, "product": prod, "price": str(safe_limit_price), "order_type": "L",
                        "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym,
                        "transaction_type": "B", "amo": "NO"
                    })

            exit_count = 0
            # Pehle Buy orders (margin free karne ke liye), phir Sell orders (As requested)
            for order in buy_exit_orders:
                print(f"Panic BUY: {order['trading_symbol']} at Limit {order['price']}")
                client.place_order(**order)
                exit_count += 1
                time.sleep(0.2) 

            for order in sell_exit_orders:
                print(f"Panic SELL: {order['trading_symbol']} at Limit {order['price']}")
                client.place_order(**order)
                exit_count += 1
                time.sleep(0.2)

            if exit_count == 0:
                return {"status": "success", "message": "No active positions to exit."}

            return {"status": "success", "message": f"🚨 Real Panic Exit Complete! {exit_count} positions squared off via Safe Limit."}

    except Exception as e:
        print(f"❌ Panic Exit Failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Panic Exit Failed: {str(e)}")


# ==========================================
# ROUTE 3: GET OPEN POSITIONS (CHART PAGE KE LIYE)
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
                            "entry_price": leg["entry_price"]
                        })
        else:
            # REAL MODE LOGIC
            users_col = get_collection("users")
            db_user = await users_col.find_one({"id": current_user["id"]})
            if db_user and db_user.get("kotak_status") == "Active":
                client = get_kotak_client(db_user)
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
# ROUTE 4: SINGLE SQUARE OFF (CHART BUTTON)
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
            # TODO: Yahan aap Kotak ka single position exit code jod sakte hain baad mein
            return {"status": "success", "message": "Real Position Square Off Logic Pending"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
