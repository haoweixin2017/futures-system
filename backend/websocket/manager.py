import asyncio
import json
import logging
from typing import Set, Dict, Any
from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()
        self.connection_feeds: Dict[WebSocket, str] = {}
        self.loop: asyncio.AbstractEventLoop = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop

    async def connect(self, websocket: WebSocket, feed: str = "sina"):
        await websocket.accept()
        self.active_connections.add(websocket)
        self.connection_feeds[websocket] = feed
        logger.info(f"WebSocket client connected. Total clients: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        self.connection_feeds.pop(websocket, None)
        logger.info("WebSocket 已断开")

    async def broadcast(self, message: Dict[str, Any]):
        dead_sockets = set()
        message_feed = message.get("feed")
        for conn in self.active_connections:
            if message_feed and self.connection_feeds.get(conn, "sina") != message_feed:
                continue
            try:
                await conn.send_json(message)
            except Exception:
                dead_sockets.add(conn)
        for dead in dead_sockets:
            self.active_connections.discard(dead)
            self.connection_feeds.pop(dead, None)

    def broadcast_threadsafe(self, message: Dict[str, Any]):
        """供同步工作线程或 CTP 回调线程无阻塞调用"""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast(message), self.loop)

ws_manager = ConnectionManager()
