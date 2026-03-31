import asyncio
import json
from fastapi import WebSocket, WebSocketDisconnect
from jose import jwt
from app.core.config import settings
from app.core.database import get_collection
from neo_api_client import NeoAPI

class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, user_email: str):
        self.active_connections[user_email] = websocket

    def disconnect(self, user_email: str):
        if user_email in self.active_connections:
            del self.active_connections[user_email]

    async def send_data(self, data: dict, user_email: str):
        if user_email in self.active_connections:
            await self.active_connections[user_email].send_text(json.dumps(data))

manager = ConnectionManager()

async def stream_live_pnl(websocket: WebSocket, token: str, mode: str = "PAPER"):
    user_email = None
    try:
        # 1. Token Decode karo
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_email = payload.get("sub")
        if not user_email:
            await websocket.close(code=1008)
            return
            
        await manager.connect(websocket, user_email)
        
        users_col = get_collection("users")
        db_user = await users_col.find_one({"email": user_email})
        if not db_user or db_user.get("kotak_status") != "Active":
            await websocket.send_text(json.dumps({"error": "Broker not connected"}))
            return
            
        client = NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
        
        # 2. CONTINUOUS LOOP (Har 3 Second)
        while True:
            total_pnl = 0.0
            formatted_positions = []

            try:
                if mode == "PAPER":
                    paper_col = get_collection("paper_trades")
                    open_trades = await paper_col.find({"user_id": db_user["id"], "status": "OPEN"}).to_list(length=100)
                    
                    # Saare tokens ikkathe karo taaki Kotak API ek hi baar call karni pade
                    tokens_to_fetch = []
                    for trade in open_trades:
                        for leg in trade.get("legs", []):
                            if leg.get("token") and leg.get("status") == "OPEN":
                                tokens_to_fetch.append({"instrument_token": str(leg["token"]), "exchange_segment": "nse_fo"})
                    
                    # Kotak se Live LTP laao
                    live_prices = {}
                    if tokens_to_fetch:
                        q = client.quotes(instrument_tokens=tokens_to_fetch, quote_type="all")
                        if q and isinstance(q, dict) and 'data' in q:
                            for item in q['data']:
                                tk = str(item.get('exchange_token') or item.get('tk'))
                                live_prices[tk] = float(item.get('ltp', item.get('lastPrice', 0)))
                    
                    # P&L Calculate karo
                    for trade in open_trades:
                        for leg in trade.get("legs", []):
                            if leg.get("status") == "OPEN":
                                ltp = live_prices.get(str(leg.get("token")), float(leg.get("entry_price", 0)))
                                entry = float(leg.get("entry_price", 0))
                                qty = int(leg.get("qty", 0))
                                
                                # Buy (Long): LTP - Entry | Sell (Short): Entry - LTP
                                mtm = (ltp - entry) * qty if leg.get("transaction") == "B" else (entry - ltp) * qty
                                total_pnl += mtm
                                
                                formatted_positions.append({
                                    "id": str(trade["_id"]),
                                    "symbol": leg["symbol"],
                                    "transaction": leg["transaction"],
                                    "qty": qty,
                                    "entry_price": entry,
                                    "ltp": ltp,
                                    "mtm": mtm
                                })

                else:
                    # REAL MODE P&L LOGIC
                    positions = client.positions()
                    if isinstance(positions, dict) and 'data' in positions:
                        for pos in positions['data']:
                            net_qty = int(pos.get('netTrdQty', pos.get('flldQty', 0)))
                            if net_qty != 0:
                                mtm = float(pos.get('mtm', 0))
                                total_pnl += mtm
                                formatted_positions.append({
                                    "id": str(pos.get('tok')),
                                    "symbol": pos.get('trdSym'),
                                    "transaction": "B" if net_qty > 0 else "S",
                                    "qty": abs(net_qty),
                                    "entry_price": float(pos.get('buyAmt', 0)) / abs(net_qty) if net_qty > 0 else float(pos.get('sellAmt', 0)) / abs(net_qty),
                                    "ltp": float(pos.get('ltp', 0)),
                                    "mtm": mtm
                                })

                # Frontend ko payload bhej do!
                await manager.send_data({
                    "pnl": total_pnl,
                    "positions": formatted_positions
                }, user_email)
                
            except Exception as e:
                print(f"WS Streaming Error: {e}")
                
            await asyncio.sleep(3) # Heavy load se bachne ke liye 3 second delay
            
    except WebSocketDisconnect:
        if user_email: manager.disconnect(user_email)
    except Exception as e:
        await websocket.close(code=1011)
