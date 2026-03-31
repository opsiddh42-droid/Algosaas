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
        await websocket.accept()
        self.active_connections[user_email] = websocket

    def disconnect(self, user_email: str):
        if user_email in self.active_connections:
            del self.active_connections[user_email]

    async def send_data(self, data: dict, user_email: str):
        if user_email in self.active_connections:
            await self.active_connections[user_email].send_text(json.dumps(data))

manager = ConnectionManager()

# Yeh function background mein hamesha chalta rahega
async def stream_live_pnl(websocket: WebSocket, token: str):
    user_email = None
    try:
        # 1. Token se User ko pehchano
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        user_email = payload.get("sub")
        if not user_email:
            await websocket.close(code=1008)
            return
            
        await manager.connect(websocket, user_email)
        
        # 2. Database se Kotak Details nikalo
        users_col = get_collection("users")
        db_user = await users_col.find_one({"email": user_email})
        
        if not db_user or db_user.get("kotak_status") != "Active":
            await websocket.send_text(json.dumps({"error": "Broker not connected"}))
            return
            
        # 3. Kotak se connection banao
        client = NeoAPI(consumer_key=db_user["kotak_consumer_key"], environment='prod')
        
        # 4. LOOP: Har 3 second mein P&L calculate karo aur Frontend ko bhejo
        while True:
            total_mtm = 0.0
            try:
                positions = client.positions()
                if isinstance(positions, dict) and 'data' in positions:
                    for pos in positions['data']:
                        buy_amt = float(pos.get('buyAmt', 0))
                        sell_amt = float(pos.get('sellAmt', 0))
                        net_qty = int(pos.get('netTrdQty', pos.get('flldQty', 0)))
                        
                        # Agar Kotak direct MTM de raha hai toh wo use karo
                        mtm = float(pos.get('mtm', 0)) 
                        
                        if mtm != 0:
                            total_mtm += mtm
                        else:
                            # Warna hum khud calculate karenge (Sell - Buy + (Live_LTP * Net_Qty))
                            if net_qty != 0:
                                token_id = pos.get('tok')
                                exch = pos.get('exSeg', 'nse_fo')
                                q = client.quotes(instrument_tokens=[{"instrument_token": str(token_id), "exchange_segment": exch}], quote_type="all")
                                
                                ltp = 0.0
                                if q and isinstance(q, dict) and 'data' in q:
                                    ltp = float(q['data'][0].get('ltp', 0))
                                elif q and isinstance(q, list):
                                    ltp = float(q[0].get('ltp', 0))
                                    
                                current_val = net_qty * ltp
                                total_mtm += (sell_amt - buy_amt) + current_val
                            else:
                                # Closed Position ka fixed profit/loss
                                total_mtm += (sell_amt - buy_amt)
                                
                # Frontend ko Data Push karo!
                await manager.send_data({"pnl": total_mtm}, user_email)
                
            except Exception as e:
                pass # Agar Kotak ka server slow ho toh chhod do, next loop me try karega
                
            await asyncio.sleep(3) # 3 second ka delay (API block na hone ke liye)
            
    except WebSocketDisconnect:
        if user_email: manager.disconnect(user_email)
    except Exception as e:
        print(f"WS Error: {e}")
        await websocket.close(code=1011)
