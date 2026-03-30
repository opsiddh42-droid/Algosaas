from fastapi import WebSocket
from typing import Dict, List
import json
import logging

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        # Yahan hum har user ka active WebSocket connection save karenge
        # Format: {"user_id": [websocket1, websocket2]} (Agar user mobile aur PC dono pe login ho)
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = []
        self.active_connections[user_id].append(websocket)
        logger.info(f"🟢 User {user_id} connected to Live Stream. Total devices: {len(self.active_connections[user_id])}")

    def disconnect(self, websocket: WebSocket, user_id: str):
        if user_id in self.active_connections:
            self.active_connections[user_id].remove(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
        logger.info(f"🔴 User {user_id} disconnected from Live Stream.")

    async def send_personal_message(self, message: dict, user_id: str):
        # Kisi ek specific user ko uska private P&L bhejna
        if user_id in self.active_connections:
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Failed to send personal message: {e}")

    async def broadcast(self, message: dict):
        # Saare users ko ek saath Global Market Data (Nifty/Sensex LTP) bhejna
        for user_id, connections in self.active_connections.items():
            for connection in connections:
                try:
                    await connection.send_text(json.dumps(message))
                except Exception as e:
                    logger.error(f"Failed to broadcast message: {e}")

# Global instance jo poore app mein use hoga
manager = ConnectionManager()
