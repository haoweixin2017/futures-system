from typing import Dict, Any, Optional
from fastapi import APIRouter, Query, HTTPException, Depends
from backend.api.auth import require_user
from backend.storage.redis_client import RedisClient
from backend.storage.mysql_client import SessionLocal
from backend.models.database import MarketBarModel
import json

# 全部端点要求已登录 (只读, 但不应匿名暴露行情与信号数据)
router = APIRouter(
    prefix="/api/v1/tools",
    tags=["ChatGPT / Codex Tools"],
    dependencies=[Depends(require_user)],
)
redis_client = RedisClient.get_client()

@router.get("/manifest")
def get_tools_manifest():
    """
    返回供 ChatGPT / Codex Agent 使用的工具定义清单 (OpenAI Function Calling Schema)
    """
    return {
        "schema_version": "v1",
        "name_for_model": "futures_realtime_analytics",
        "description_for_model": "国内期货只读实时行情、多周期K线与客观规则信号工具。所有指标和信号严格由底层程序客观计算，不可篡改。",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_futures_quote",
                    "description": "获取指定期货合约的最新Tick行情快照（最新价、买一卖一、成交量、持仓量、更新时间）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instrument": {"type": "string", "description": "合约代码，如 JM2701, FU2610, BU2610"}
                        },
                        "required": ["instrument"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_futures_bars",
                    "description": "获取指定期货合约的多周期历史K线（含OHLCV及持仓变化）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instrument": {"type": "string", "description": "合约代码，如 JM2701"},
                            "period": {"type": "string", "enum": ["1m", "5m", "15m", "30m", "60m", "1d"], "default": "15m"},
                            "limit": {"type": "integer", "description": "返回K线根数，默认100", "default": 100}
                        },
                        "required": ["instrument"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_futures_signal",
                    "description": "获取指定期货合约当前由规则引擎生成的客观信号、评分、触发价与失效价",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instrument": {"type": "string", "description": "合约代码，如 JM2701"}
                        },
                        "required": ["instrument"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "explain_futures_signal",
                    "description": "获取指定合约规则信号的详细数值追溯原因，包含突破价位、放量倍数、持仓增量等客观条件判定结果",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "instrument": {"type": "string", "description": "合约代码，如 JM2701"}
                        },
                        "required": ["instrument"]
                    }
                }
            }
        ]
    }

@router.get("/get_futures_quote")
def get_futures_quote(instrument: str = Query(..., description="合约代码，如 JM2701")):
    data = redis_client.hgetall(f"quote:{instrument}")
    if not data:
        raise HTTPException(status_code=404, detail=f"合约 {instrument} 暂无行情数据")
    return {
        "status": "success",
        "instrument": instrument,
        "quote": data
    }

@router.get("/get_futures_bars")
def get_futures_bars(
    instrument: str = Query(..., description="合约代码"),
    period: str = Query("15m", pattern="^(1m|5m|15m|30m|60m|1d)$"),
    limit: int = Query(100, ge=1, le=500)
):
    key = f"bars:{instrument}:{period}"
    raw_bars = redis_client.lrange(key, -limit, -1)
    if raw_bars:
        bars = [json.loads(b) for b in raw_bars]
        return {
            "status": "success",
            "instrument": instrument,
            "period": period,
            "bars": bars
        }

    session = SessionLocal()
    try:
        rows = session.query(MarketBarModel).filter_by(
            instrument_id=instrument,
            period=period
        ).order_by(MarketBarModel.bar_time.desc()).limit(limit).all()
        rows.reverse()
        bars = [
            {
                "instrument": r.instrument_id,
                "trading_day": r.trading_day,
                "period": r.period,
                "bar_time": r.bar_time.isoformat(),
                "open_price": str(r.open_price),
                "high_price": str(r.high_price),
                "low_price": str(r.low_price),
                "close_price": str(r.close_price),
                "volume": r.volume,
                "turnover": str(r.turnover),
                "open_interest": str(r.open_interest),
                "open_interest_change": str(r.open_interest_change)
            }
            for r in rows
        ]
        return {
            "status": "success",
            "instrument": instrument,
            "period": period,
            "bars": bars
        }
    finally:
        session.close()

@router.get("/get_futures_signal")
def get_futures_signal(instrument: str = Query(..., description="合约代码")):
    sig = redis_client.hgetall(f"signal:{instrument}")
    if not sig:
        return {
            "status": "success",
            "instrument": instrument,
            "signal": "NONE",
            "rule_score": 0,
            "message": "当前暂无触发的规则信号"
        }
    if "reasons" in sig:
        sig["reasons"] = json.loads(sig["reasons"])
    return {
        "status": "success",
        "signal_data": sig
    }

@router.get("/explain_futures_signal")
def explain_futures_signal(instrument: str = Query(..., description="合约代码")):
    sig = redis_client.hgetall(f"signal:{instrument}")
    if not sig:
        return {
            "status": "success",
            "instrument": instrument,
            "has_signal": False,
            "summary": f"合约 {instrument} 目前各项技术条件处于正常波动，未触发客观规则观察阈值。"
        }
    reasons = json.loads(sig.get("reasons", "[]"))
    passed = [r for r in reasons if r.get("passed")]
    score = int(sig.get("rule_score", 0))

    summary = (
        f"【客观规则诊断】合约 {instrument} 在 {sig.get('timeframe')} 周期触发 {sig.get('signal')} 观察信号。"
        f"客观规则评分: {score}/100。"
        f"触发价: {sig.get('trigger_price')}，失效防守价: {sig.get('invalid_price')}。"
        f"符合条件的客观规则共 {len(passed)} 项。"
    )

    return {
        "status": "success",
        "instrument": instrument,
        "signal": sig.get("signal"),
        "rule_score": score,
        "summary": summary,
        "details": reasons
    }

