from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from backend.api.routes import app
from backend.api.tools_api import router as tools_router
import backend.websocket.routes

# 挂载 ChatGPT / Codex 工具路由
app.include_router(tools_router)
