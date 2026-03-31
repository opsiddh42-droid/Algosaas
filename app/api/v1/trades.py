from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from neo_api_client import NeoAPI
from app.api.deps import get_current_user
from app.core.database import get_collection
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
# ROUTE 2: PANIC EXIT ALL (SMART BUFFER LIMIT)
# ==========================================
@router.post("/exit-all")
async def panic_exit_all(current_user: dict = Depends(get_current_user)):
    try:
        users_col = get_collection("users")
        db_user = await users_col.find_one({"id": current_user["id"]})

        if not db_user or db_user.get("kotak_status") != "Active":
            raise HTTPException(status_code=400, detail="Broker profile not connected.")

        client = get_kotak_client(db_user)
        print("🚨 PANIC EXIT INITIATED! Fetching open positions...")

        # 1. Live positions khincho
        positions_response = client.positions()
        
        if not isinstance(positions_response, dict) or 'data' not in positions_response:
            return {"status": "success", "message": "No open positions found to exit."}

        open_positions = positions_response['data']
        
        buy_exit_orders = []   
        sell_exit_orders = []  

        # 2. Process Positions & Calculate Buffered Limit Price
        for pos in open_positions:
            net_qty = int(pos.get('netTrdQty', pos.get('flldQty', 0)))
            if net_qty == 0:
                continue 
                
            sym = pos.get('trdSym', pos.get('trading_symbol', ''))
            exch = pos.get('exSeg', pos.get('exchange_segment', 'nse_fo'))
            prod = pos.get('prd', pos.get('product', 'NRML'))
            token = pos.get('tok', pos.get('instrument_token', ''))

            # ✅ Live LTP khinchna zaroori hai Panic Limit Order ke liye
            ltp = 0.0
            try:
                if token:
                    q = client.quotes(instrument_tokens=[{"instrument_token": str(token), "exchange_segment": exch}], quote_type="all")
                    quote_data = q.get('data', []) if isinstance(q, dict) else q
                    ltp = float(quote_data[0].get('ltp', quote_data[0].get('lastPrice', 0)))
            except:
                pass # Agar LTP fail hua toh humein bachao price dhoondhna padega

            if ltp == 0:
                ltp = float(pos.get('buyAmt', 0)) # Fallback if live LTP fails

            if net_qty > 0:
                # Hum Long hain -> SELL karna hai -> Price thoda NEECHE lagayenge (taaki turant execute ho)
                safe_limit_price = round(ltp * 0.85, 1) # 15% neechay ka Limit (Instant Market fill ke liye)
                
                sell_exit_orders.append({
                    "exchange_segment": exch, "product": prod, "price": str(safe_limit_price), "order_type": "L",
                    "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym,
                    "transaction_type": "S", "amo": "NO"
                })
            elif net_qty < 0:
                # Hum Short hain -> BUY karna hai -> Price thoda UPAR lagayenge
                safe_limit_price = round(ltp * 1.15, 1) # 15% upar ka Limit
                
                buy_exit_orders.append({
                    "exchange_segment": exch, "product": prod, "price": str(safe_limit_price), "order_type": "L",
                    "quantity": str(abs(net_qty)), "validity": "DAY", "trading_symbol": sym,
                    "transaction_type": "B", "amo": "NO"
                })

        exit_count = 0

        # 3. PRIORITY EXECUTION: Pehle Buy orders (margin free karne ke liye), phir Sell orders
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

        return {"status": "success", "message": f"🚨 Panic Exit Complete! {exit_count} positions squared off via Safe Limit."}

    except Exception as e:
        print(f"❌ Panic Exit Failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Panic Exit Failed: {str(e)}")
