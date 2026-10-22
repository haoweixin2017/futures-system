from fastapi import WebSocket, WebSocketDisconnect
from backend.api.routes import app
from backend.websocket.manager import ws_manager
from backend.api.auth import get_current_user

@app.websocket("/api/v1/ws/market")
async def websocket_endpoint(websocket: WebSocket):
    user = get_current_user(websocket)
    feed = "ctp" if user and user.can_live_trade else "sina"
    await ws_manager.connect(websocket, feed=feed)
    try:
        while True:
            # 接收客户端心跳 ping 或指令
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)
