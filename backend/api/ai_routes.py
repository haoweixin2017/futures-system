import hashlib
import json
import threading
import uuid
from datetime import datetime, timedelta
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from backend.ai_analysis import (credentials, orchestrator, provider, registry, snapshot, settlement,
                                 research_cache, research_scheduler)
from backend.api.auth import require_user
from backend.common.trading_time import FuturesTradingCalendar
from backend.common.product_specs import extract_product
from backend.models.database import (
    AIAnalysisBatchModel, AIAnalysisCallModel, AIAnalysisEvaluationModel,
    AIAnalysisFollowUpModel, AIAnalysisReviewSummaryModel,
    AIAnalysisReviewUsageModel, AIAnalysisRunModel,
)
from backend.storage.mysql_client import SessionLocal
from backend.storage.redis_client import RedisClient

router = APIRouter(prefix="/api/v1/ai", tags=["ai-analysis"])

# 分析/追问/复盘各自占用模型调用与线程池资源, 无上限时单个用户可无限刷任务。
MAX_ACTIVE_BATCHES_PER_USER = 5
MAX_QUEUED_RUNS = 50
MAX_ACTIVE_FOLLOWUPS_PER_USER = 5
MAX_ACTIVE_REVIEWS_PER_USER = 5

_batch_gate = threading.Lock()
_followup_gate = threading.Lock()
_review_gate = threading.Lock()


class ModelConfigBody(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)
    provider: str = "openai_compatible"
    base_url: str = Field(min_length=8, max_length=512)
    model_name: str = Field(min_length=1, max_length=128)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    requires_api_key: bool = True
    is_local_service: bool = False
    max_context_tokens: int = Field(default=32768, ge=1024, le=2_000_000)
    max_output_tokens: int = Field(default=2000, ge=128, le=65536)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    temperature: float = Field(default=0.1, ge=0, le=2)
    enabled: bool = True

    @model_validator(mode="after")
    def validate_combination(self):
        if self.provider not in {"openai_compatible", "google_gemini"}:
            raise ValueError("仅支持 openai_compatible 或 google_gemini")
        if self.max_output_tokens >= self.max_context_tokens:
            raise ValueError("最大输出 Token 必须小于最大上下文")
        return self


class ModelEnabledBody(BaseModel):
    enabled: bool


class AnalysisCreateBody(BaseModel):
    client_request_id: uuid.UUID
    model_config_ids: List[uuid.UUID] = Field(min_length=1, max_length=5)
    analysis_target: Literal["TODAY", "CURRENT_SESSION", "NEXT_OPEN", "FUTURE_TREND"]
    analysis_requirements: str = Field(default="", max_length=2000)
    session_start_time: Optional[str] = Field(default=None, max_length=32)
    use_review_summaries: bool = True
    allow_insufficient_data: bool = False
    self_consistency_votes: int = Field(default=1, ge=1, le=3,
                                        description="同模型投票次数: 1=单次, 3=三重投票取方向多数")

    @model_validator(mode="after")
    def validate_session_selection(self):
        if self.analysis_target == "CURRENT_SESSION" and not self.session_start_time:
            raise ValueError("盘时段分析必须选择交易时段")
        return self


class PromptPreviewBody(BaseModel):
    analysis_target: Literal["TODAY", "CURRENT_SESSION", "NEXT_OPEN", "FUTURE_TREND"]
    analysis_requirements: str = Field(default="", max_length=2000)
    instrument: str = Field(default="", max_length=32)
    session_start_time: Optional[str] = Field(default=None, max_length=32)
    use_review_summaries: bool = True


class EvaluationBody(BaseModel):
    rating: Literal["CORRECT", "PARTIAL", "WRONG", "UNVERIFIABLE"]
    comment: str = Field(default="", max_length=1000)


class FollowUpBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    attach_latest_market: bool = False


class ReviewSummaryBody(BaseModel):
    generator_model_config_id: uuid.UUID
    scope: Literal["CONTRACT", "PRODUCT"] = "CONTRACT"
    user_guidance: str = Field(default="", max_length=4000)


class ResearchContractBody(BaseModel):
    instrument: str = Field(min_length=1, max_length=32)
    related: List[str] = Field(default_factory=list, max_length=0)


class ResearchSettingsBody(BaseModel):
    enabled: bool = False
    # None 表示本次请求未提交该字段（保留用户原值），显式 true/false 才覆盖。
    ai_enabled: Optional[bool] = None
    model_config_id: Optional[uuid.UUID] = None
    interval_minutes: Literal[15, 30, 60, 120] = 60
    contracts: List[ResearchContractBody] = Field(default_factory=list)
    config_version: int = Field(default=0, ge=0)


class ResearchRefreshBody(BaseModel):
    instrument: Optional[str] = Field(default=None, min_length=1, max_length=32)


EVALUATION_SCORES = {"CORRECT": 100, "PARTIAL": 50, "WRONG": 0, "UNVERIFIABLE": None}


def _sim_user(request: Request):
    user = require_user(request)
    if user.role != "sim":
        raise HTTPException(status_code=403, detail="AI 分析第一版仅供模拟用户使用")
    return user


def _raise_config_error(exc: Exception):
    # CredentialError 是 RuntimeError 的子类, 必须先判断才能命中 503
    if isinstance(exc, credentials.CredentialError):
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if isinstance(exc, LookupError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, RuntimeError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/research/settings")
def get_research_settings(request: Request):
    user = _sim_user(request)
    return research_cache.get_settings(user.username)


@router.put("/research/settings")
def put_research_settings(body: ResearchSettingsBody, request: Request):
    user = _sim_user(request)
    try:
        return research_cache.save_settings(user.username, body.model_dump(mode="json"))
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


@router.post("/research/refresh")
def refresh_research(body: ResearchRefreshBody, request: Request):
    user = _sim_user(request)
    try:
        return research_scheduler.enqueue_refresh(user.username, body.instrument)
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


@router.get("/research/status")
def get_research_status(request: Request, instrument: Optional[str] = None, all_contracts: bool = False):
    user = _sim_user(request)
    try:
        return research_cache.get_status(user.username, instrument, all_contracts=True) if all_contracts else research_cache.get_status(user.username, instrument)
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


@router.get("/research/materials")
def list_research_materials(request: Request,
                            topic: Optional[str] = None,
                            dimension: Optional[str] = None,
                            method: Optional[str] = None,
                            series: Optional[str] = None,
                            q: Optional[str] = None,
                            instrument: Optional[str] = None,
                            cutoff: Optional[str] = None,
                            history_for: Optional[str] = None,
                            page: int = Query(1, ge=1),
                            page_size: int = Query(20, ge=1, le=100)):
    user = _sim_user(request)
    try:
        return research_cache.list_materials(user.username, topic=topic, dimension=dimension,
            method=method, series=series, q=q, page=page, page_size=page_size,
            instrument=instrument, cutoff=cutoff, history_for=history_for)
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


@router.get("/research/materials/export")
def export_research_materials(request: Request,
                              topic: Optional[str] = None,
                              dimension: Optional[str] = None,
                              method: Optional[str] = None,
                              series: Optional[str] = None,
                              q: Optional[str] = None,
                              instrument: Optional[str] = None,
                              limit: int = Query(500, ge=1, le=1000)):
    """按筛选导出含正文的资料记录 (JSON, 与录入/导入格式兼容)。"""
    user = _sim_user(request)
    try:
        return research_cache.export_materials(user.username, topic=topic, dimension=dimension,
            method=method, series=series, q=q, instrument=instrument, limit=limit)
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


@router.get("/research/materials/{material_id}")
def get_research_material(material_id: str, request: Request):
    user = _sim_user(request)
    try:
        return research_cache.get_material(user.username, material_id)
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


class ManualMaterialRecord(BaseModel):
    target: str = Field(min_length=1, max_length=16, description="目标合约代码或 macro(公共宏观)")
    dimension: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=50000)
    url: Optional[str] = Field(default=None, max_length=512)
    source_name: Optional[str] = Field(default=None, max_length=64)
    published_at: Optional[str] = Field(default=None, max_length=32)
    data_period: Optional[str] = Field(default=None, max_length=64)


class ManualMaterialsBody(BaseModel):
    records: List[ManualMaterialRecord] = Field(min_length=1, max_length=200)


@router.post("/research/materials/manual")
def create_manual_materials(payload: ManualMaterialsBody, request: Request):
    """手工录入/批量导入资料: 来源标记为 MANUAL, 并生成已发布的手工采集检查。"""
    user = _sim_user(request)
    try:
        return research_cache.persist_manual_materials(
            user.username, [record.model_dump() for record in payload.records], datetime.now())
    except (ValueError, LookupError, RuntimeError) as exc:
        _raise_config_error(exc)


def _session_key(snapshot_value: dict) -> str | None:
    target = snapshot_value.get("session_target") or {}
    start, end = target.get("start_time"), target.get("end_time")
    return f"{str(start)[11:16]}-{str(end)[11:16]}" if start and end else None


def _matching_review_context(session, owner: str, instrument: str, analysis_target: str,
                             session_key: str | None = None) -> dict:
    product = extract_product(instrument)
    rows = session.query(AIAnalysisReviewSummaryModel).filter_by(
        owner_username=owner, analysis_target=analysis_target, status="ACTIVE").all()
    matched = [row for row in rows if (
        (row.scope == "CONTRACT" and row.instrument_id == instrument)
        or (row.scope == "PRODUCT" and row.product_code == product)
    ) and (analysis_target != "CURRENT_SESSION" or row.session_key == session_key)]
    matched.sort(key=lambda row: (
        0 if row.scope == "CONTRACT" and row.instrument_id == instrument else 1,
        -(row.activated_at or row.created_at).timestamp(),
    ))
    selected, used_tokens = [], 0
    for row in matched:
        try:
            summary = json.loads(row.summary_json or "{}")
        except json.JSONDecodeError:
            continue
        evaluation = session.query(AIAnalysisEvaluationModel).filter_by(
            evaluation_id=row.evaluation_id).first()
        item = {
            "summary_id": row.summary_id, "source_instrument": row.instrument_id,
            "analysis_target": row.analysis_target, "scope": row.scope,
            "rating": evaluation.rating if evaluation else None, "summary": summary,
        }
        estimate = orchestrator.estimate_tokens(item)
        if len(selected) >= 10 or used_tokens + estimate > 1500:
            continue
        selected.append(item)
        used_tokens += estimate
    return {
        "matched_count": len(matched), "used_count": len(selected),
        "token_estimate": used_tokens, "summaries": selected,
    }


@router.get("/models")
def get_models(request: Request):
    user = _sim_user(request)
    return {"models": registry.list_models(user.username), "max_selected": 5}


@router.post("/models", status_code=201)
def post_model(body: ModelConfigBody, request: Request):
    user = _sim_user(request)
    try:
        return registry.create_model(user.username, body.model_dump())
    except (ValueError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.put("/models/{model_config_id}")
def put_model(model_config_id: str, body: ModelConfigBody, request: Request):
    user = _sim_user(request)
    try:
        uuid.UUID(model_config_id)
        return registry.update_model(user.username, model_config_id, body.model_dump())
    except (ValueError, LookupError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.patch("/models/{model_config_id}/enabled")
def patch_model_enabled(model_config_id: str, body: ModelEnabledBody, request: Request):
    user = _sim_user(request)
    try:
        uuid.UUID(model_config_id)
        return registry.set_model_enabled(user.username, model_config_id, body.enabled)
    except (ValueError, LookupError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.delete("/models/{model_config_id}")
def remove_model(model_config_id: str, request: Request):
    user = _sim_user(request)
    try:
        uuid.UUID(model_config_id)
        registry.delete_model(user.username, model_config_id)
        return {"status": "deleted"}
    except (ValueError, LookupError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.post("/models/{model_config_id}/clear-key")
def remove_model_key(model_config_id: str, request: Request):
    user = _sim_user(request)
    try:
        uuid.UUID(model_config_id)
        return registry.clear_key(user.username, model_config_id)
    except (ValueError, LookupError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.post("/models/{model_config_id}/test")
def test_model(model_config_id: str, request: Request):
    user = _sim_user(request)
    try:
        uuid.UUID(model_config_id)
        return orchestrator.test_model_connection(user.username, model_config_id)
    except (ValueError, LookupError, RuntimeError, credentials.CredentialError) as exc:
        _raise_config_error(exc)


@router.post("/analysis/prompt-preview")
def analysis_prompt_preview(body: PromptPreviewBody, request: Request):
    user = _sim_user(request)
    instrument = body.instrument.strip().upper()
    result = orchestrator.instruction_preview(
        body.analysis_target, body.analysis_requirements.strip(), body.instrument.strip().upper(),
        body.session_start_time)
    session_key = _session_key(result) if body.analysis_target == "CURRENT_SESSION" else None
    session = SessionLocal()
    try:
        context = _matching_review_context(
            session, user.username, instrument, body.analysis_target, session_key) if instrument else {
                "matched_count": 0, "used_count": 0, "token_estimate": 0, "summaries": []}
    finally:
        session.close()
    result["review_context"] = {
        "enabled": body.use_review_summaries,
        **(context if body.use_review_summaries else {
            "matched_count": context["matched_count"], "used_count": 0,
            "token_estimate": 0, "summaries": []}),
    }
    return result


@router.get("/analysis/review-context/{instrument}")
def applicable_review_context(instrument: str, request: Request, analysis_target: str = "TODAY",
                              session_start_time: Optional[str] = None):
    user = _sim_user(request)
    instrument = instrument.strip().upper()
    if analysis_target not in {"TODAY", "CURRENT_SESSION", "NEXT_OPEN", "FUTURE_TREND"}:
        raise HTTPException(status_code=422, detail="分析类型无效")
    session_key = None
    if analysis_target == "CURRENT_SESSION":
        options = FuturesTradingCalendar.analysis_session_options(datetime.now(), instrument)
        selected = next((item for item in options if item["start_time"] == session_start_time), None)
        session_key = _session_key({"session_target": selected}) if selected else None
    session = SessionLocal()
    try:
        return _matching_review_context(
            session, user.username, instrument, analysis_target, session_key)
    finally:
        session.close()


@router.get("/analysis/session-options/{instrument}")
def analysis_session_options(instrument: str, request: Request):
    _sim_user(request)
    instrument = instrument.strip().upper()
    if not instrument or len(instrument) > 32:
        raise HTTPException(status_code=422, detail="合约代码无效")
    return {"instrument": instrument,
            "sessions": FuturesTradingCalendar.analysis_session_options(datetime.now(), instrument)}


@router.get("/analysis/availability/{instrument}")
def analysis_availability(instrument: str, request: Request):
    _sim_user(request)
    instrument = instrument.strip().upper()
    if not instrument or len(instrument) > 32:
        raise HTTPException(status_code=422, detail="合约代码无效")
    redis_client = RedisClient.get_client()
    quote = (redis_client.hgetall(f"quote:sina:{instrument}") or
             redis_client.hgetall(f"quote:{instrument}"))
    available = bool(quote) and FuturesTradingCalendar.today_analysis_available(
        datetime.now(), instrument, str(quote.get("trading_day") or ""))
    return {"instrument": instrument, "today_available": available,
            "today_reason": "当前交易日已结束、尚未开始或缺少当日行情，不能分析今日走势" if not available else ""}


@router.post("/analysis/{instrument}", status_code=202)
def create_analysis(instrument: str, body: AnalysisCreateBody, request: Request):
    user = _sim_user(request)
    return create_analysis_for_owner(user.username, instrument, body)


def create_analysis_for_owner(owner_username: str, instrument: str, body: AnalysisCreateBody):
    """已认证 API 或用户明确启动的模拟验证复用同一冻结/限额入口。"""
    instrument = instrument.strip().upper()
    if not instrument or len(instrument) > 32:
        raise HTTPException(status_code=422, detail="合约代码无效")
    request_id = str(body.client_request_id)
    model_ids = list(dict.fromkeys(str(value) for value in body.model_config_ids))
    session = SessionLocal()
    created_new = True
    try:
        existing = session.query(AIAnalysisBatchModel).filter_by(
            owner_username=owner_username, client_request_id=request_id).first()
        if existing:
            return _batch_summary(existing, session)
    finally:
        session.close()
    if (body.analysis_target == "TODAY" and
            not FuturesTradingCalendar.today_analysis_available(datetime.now(), instrument)):
        raise HTTPException(status_code=422, detail="当前交易日已结束或尚未开始，不能分析今日走势")
    configs = []
    try:
        for model_config_id in model_ids:
            configs.append(registry.model_runtime_config(owner_username, model_config_id))
    except (ValueError, LookupError, credentials.CredentialError) as exc:
        _raise_config_error(exc)
    try:
        snap, snap_hash = snapshot.build_snapshot(
            instrument, body.analysis_target, body.analysis_requirements.strip(),
            body.session_start_time)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if (body.analysis_target == "TODAY" and
            not FuturesTradingCalendar.today_analysis_available(
                datetime.now(), instrument, snap["market"]["trading_day"])):
        raise HTTPException(status_code=422, detail="行情不属于当前可分析的交易日，不能分析今日走势")
    if (not snap["quality"]["bar_count"] and body.analysis_target != "CURRENT_SESSION"
            and not body.allow_insufficient_data):
        raise HTTPException(status_code=422, detail="当日没有已闭合 1 分钟 K 线")
    if body.analysis_target == "FUTURE_TREND":
        try:
            public = research_cache.freeze_materials(
                owner_username, instrument, snap["market"]["snapshot_time"], snap.get("future_targets"))
            snap["public_materials"] = public
            snap["related_contracts"] = []
            snap["related_market_snapshots"] = []
            snap["quality"]["warnings"] = list(dict.fromkeys(snap["quality"].get("warnings", []) +
                ["MISSING_" + value.upper() for value in public.get("missing_dimensions", [])]))
        except (ValueError, LookupError, RuntimeError) as exc:
            _raise_config_error(exc)
    review_context = {"matched_count": 0, "used_count": 0, "token_estimate": 0, "summaries": []}
    if body.use_review_summaries:
        session = SessionLocal()
        try:
            review_context = _matching_review_context(
                session, owner_username, instrument, body.analysis_target, _session_key(snap))
        finally:
            session.close()
    snap["review_context"] = {"enabled": body.use_review_summaries, **review_context}
    canonical = json.dumps(snap, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snap_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    batch_id = str(uuid.uuid4())
    now = datetime.utcnow()
    snapshot_time = datetime.fromisoformat(snap["market"]["snapshot_time"]).replace(tzinfo=None)
    session = SessionLocal()
    try:
        with _batch_gate:
            active_batches = session.query(AIAnalysisBatchModel.id).filter(
                AIAnalysisBatchModel.owner_username == owner_username,
                AIAnalysisBatchModel.status.in_(["CREATED", "RUNNING"])).count()
            if active_batches >= MAX_ACTIVE_BATCHES_PER_USER:
                raise HTTPException(status_code=429, detail=(
                    f"已有 {active_batches} 个分析批次在进行中，最多同时进行 "
                    f"{MAX_ACTIVE_BATCHES_PER_USER} 个，请等待完成后再发起"))
            queued_runs = session.query(AIAnalysisRunModel.id).filter(
                AIAnalysisRunModel.status.in_(["PENDING", "RUNNING"])).count()
            if queued_runs + len(model_ids) > MAX_QUEUED_RUNS:
                raise HTTPException(status_code=429, detail="分析任务排队已满，请稍后重试")
            batch = AIAnalysisBatchModel(
                batch_id=batch_id, client_request_id=request_id, owner_username=owner_username,
                instrument_id=instrument, instrument_name=snap["instrument"]["name"], feed="sina",
                trading_day=snap["market"]["trading_day"], snapshot_time=snapshot_time,
                snapshot_hash=snap_hash, snapshot_json=json.dumps(snap, ensure_ascii=False, default=str),
                selected_models_json=json.dumps(model_ids), status="CREATED", created_at=now,
            )
            session.add(batch)
            run_ids = []
            for config in configs:
                # 投票次数属于本次分析请求, 随运行冻结配置落库, orchestrator 读取
                public_config = {**registry.public_snapshot(config),
                                 "self_consistency_votes": body.self_consistency_votes}
                run_id = str(uuid.uuid4())
                run_ids.append(run_id)
                session.add(AIAnalysisRunModel(
                    run_id=run_id, batch_id=batch_id,
                    model_config_id=config["model_config_id"], model_display_name=config["display_name"],
                    provider=config["provider"], model_name=config["model_name"],
                    base_url_snapshot=config["base_url"],
                    config_json=json.dumps(public_config, ensure_ascii=False),
                    config_hash=registry.config_hash(config), status="PENDING", created_at=now,
                ))
            for run_id in run_ids:
                for sequence, item in enumerate(review_context["summaries"], 1):
                    frozen = json.dumps(item, ensure_ascii=False, sort_keys=True)
                    session.add(AIAnalysisReviewUsageModel(
                        run_id=run_id, summary_id=item["summary_id"], sequence_no=sequence,
                        summary_hash=hashlib.sha256(frozen.encode("utf-8")).hexdigest(),
                        summary_snapshot_json=frozen, created_at=now,
                    ))
            session.commit()
            session.refresh(batch)
            result = _batch_summary(batch, session)
    except IntegrityError:
        session.rollback()
        created_new = False
        batch = session.query(AIAnalysisBatchModel).filter_by(
            owner_username=owner_username, client_request_id=request_id).one()
        result = _batch_summary(batch, session)
        batch_id = batch.batch_id
    finally:
        session.close()
    if created_new:
        orchestrator.submit_batch(batch_id)
    return result


def _legacy_compatible_result(run: AIAnalysisRunModel, session, expected_instrument: str):
    if run.status != "UNSTRUCTURED" or run.parsed_response_json:
        return None
    if getattr(run, 'batch_id', None):
        batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=run.batch_id).first()
        if batch and json.loads(batch.snapshot_json).get('schema_version') == '1.1':
            return None  # 新契约校验失败不能再经旧格式兼容恢复方向结论。
    call = session.query(AIAnalysisCallModel).filter_by(run_id=run.run_id, status="SUCCEEDED").order_by(
        AIAnalysisCallModel.sequence_no.desc()).first()
    if not call or not call.raw_response_text:
        return None
    try:
        content = provider.response_content(call.raw_response_text, run.provider)
        parsed = provider.parse_analysis_json(content, require_forecast=False)
        if provider.analysis_instrument(parsed) != expected_instrument:
            return None
        parsed["instrument"] = expected_instrument
        return parsed
    except (ValueError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None


def _review_summary(row: AIAnalysisReviewSummaryModel, include_raw: bool = False):
    try:
        parsed = json.loads(row.summary_json) if row.summary_json else None
    except json.JSONDecodeError:
        parsed = None
    return {
        "summary_id": row.summary_id, "run_id": row.run_id,
        "evaluation_id": row.evaluation_id, "revision_no": row.revision_no,
        "source_model_config_id": row.source_model_config_id,
        "generator_model_config_id": row.generator_model_config_id,
        "instrument": row.instrument_id, "product_code": row.product_code,
        "analysis_target": row.analysis_target, "session_key": row.session_key,
        "scope": row.scope, "user_guidance": row.user_guidance or "",
        "summary": parsed, "status": row.status,
        "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "latency_ms": row.latency_ms, "error": row.sanitized_error,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        "activated_at": row.activated_at.isoformat() if row.activated_at else None,
        "disabled_at": row.disabled_at.isoformat() if row.disabled_at else None,
        **({"request": json.loads(row.request_json) if row.request_json else None,
            "raw_response_text": row.raw_response_text} if include_raw else {}),
    }


def _run_summary(run: AIAnalysisRunModel, session=None, expected_instrument: str = ""):
    parsed = json.loads(run.parsed_response_json) if run.parsed_response_json else None
    from backend.ai_analysis.session_window import chart_for_completion, completion_time
    if isinstance(parsed, dict) and isinstance(parsed.get('session_chart'), dict):
        parsed['session_chart'] = chart_for_completion(parsed['session_chart'], run.completed_at)
    legacy_compatible = False
    if parsed is None and session is not None and expected_instrument:
        parsed = _legacy_compatible_result(run, session, expected_instrument)
        legacy_compatible = parsed is not None
    evaluation = None
    review_summaries = []
    if session is not None:
        row = session.query(AIAnalysisEvaluationModel).filter_by(run_id=run.run_id).first()
        if row:
            evaluation = _evaluation_summary(row)
        review_summaries = [_review_summary(item) for item in session.query(
            AIAnalysisReviewSummaryModel).filter_by(run_id=run.run_id).order_by(
                AIAnalysisReviewSummaryModel.revision_no.desc()).all()]
    usage = []
    if session is not None:
        for call in session.query(AIAnalysisCallModel).filter_by(run_id=run.run_id).order_by(
                AIAnalysisCallModel.sequence_no.asc()).all():
            sent = orchestrator.sent_review_summaries(json.loads(call.request_json or '{}'))
            usage.append({'sequence': call.sequence_no, 'call_type': call.call_type,
                          'summary_ids': [item['summary_id'] for item in sent]})
    return {
        "run_id": run.run_id, "model_config_id": run.model_config_id,
        "model_display_name": run.model_display_name, "model_name": run.model_name,
        "status": run.status, "execution_mode": run.execution_mode,
        "completed_at": completion_time(run.completed_at).isoformat() if run.completed_at else None,
        "result": parsed,
        "review_status": (parsed or {}).get('review_status', 'UNKNOWN'),
        "review_error": (parsed or {}).get('review_error'),
        "review_summary_calls": usage,
        "used_review_summary_ids": sorted({sid for call in usage for sid in call['summary_ids']}),
        "direction": run.direction or (parsed.get("direction") if parsed else None),
        "confidence_score": (run.confidence_score if run.confidence_score is not None
                             else parsed.get("confidence_score") if parsed else None),
        "call_count": run.call_count, "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens, "latency_ms": run.latency_ms,
        "error": run.sanitized_error, "legacy_compatible": legacy_compatible,
        "evaluation": evaluation,
        "review_summaries": review_summaries,
    }


def _evaluation_summary(row: AIAnalysisEvaluationModel):
    return {
        "evaluation_id": row.evaluation_id, "rating": row.rating, "score": row.score,
        "comment": row.comment or "", "evaluated_at": row.evaluated_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _model_target_stats(session, owner: str, model_config_id: str, analysis_target: str):
    rows = session.query(AIAnalysisEvaluationModel).join(
        AIAnalysisRunModel, AIAnalysisRunModel.run_id == AIAnalysisEvaluationModel.run_id).filter(
            AIAnalysisEvaluationModel.owner_username == owner,
            AIAnalysisEvaluationModel.analysis_target == analysis_target,
            AIAnalysisRunModel.model_config_id == model_config_id,
        ).all()
    manual = [row for row in rows if (not (row.comment or '').startswith(settlement.AUTO_SETTLE_TAG)
              or ' → 人工改判为' in (row.comment or ''))
              and row.score is not None]
    metrics = []
    legacy_count = 0
    for row in rows:
        run = session.query(AIAnalysisRunModel).filter_by(run_id=row.run_id).one()
        value = json.loads(run.parsed_response_json or '{}').get('settlement_metrics')
        if value:
            metrics.append(value)
        elif (row.comment or "").startswith(settlement.AUTO_SETTLE_TAG) and " → 人工改判为" not in (row.comment or ""):
            legacy_count += 1
    directional = [m for m in metrics if m.get('direction_rating') in {'CORRECT', 'WRONG'}]
    targets = [m for m in metrics if m.get('target_state') in {'HIT', 'INVALIDATED', 'PENDING'}]
    unknown = sum(m.get('direction_rating') not in {'CORRECT', 'WRONG'} for m in metrics)
    counts = {rating: sum(row.rating == rating for row in rows) for rating in EVALUATION_SCORES}
    return {
        'analysis_target': analysis_target,
        'direction_accuracy': round(100 * sum(m['direction_rating'] == 'CORRECT' for m in directional)
                                    / len(directional), 1) if directional else None,
        'direction_count': len(directional),
        'target_first_touch_rate': round(100 * sum(m['target_state'] == 'HIT' for m in targets)
                                        / len(targets), 1) if targets else None,
        'target_count': len(targets),
        'manual_score': round(sum(row.score for row in manual) / len(manual), 1) if manual else None,
        'manual_count': len(manual),
        'unverifiable_rate': round(100 * unknown / len(metrics), 1) if metrics else None,
        'unverifiable_count': unknown, 'automatic_count': len(metrics),
        'legacy_count': legacy_count,
        'evaluation_count': len(rows), 'counts': counts,
    }



def _batch_summary(batch: AIAnalysisBatchModel, session):
    runs = session.query(AIAnalysisRunModel).filter_by(batch_id=batch.batch_id).order_by(
        AIAnalysisRunModel.created_at.asc()).all()
    snap = json.loads(batch.snapshot_json)
    review_context = snap.get("review_context") or {
        "enabled": False, "matched_count": 0, "used_count": 0, "summaries": []}
    line_states = settlement.session_line_states(session, batch, runs, snap) \
        if snap.get("analysis_target") == "CURRENT_SESSION" else {}
    display_until = None
    if snap.get("analysis_target") == "CURRENT_SESSION" and (snap.get("session_target") or {}).get("end_time"):
        try:
            session_end = datetime.fromisoformat(snap["session_target"]["end_time"]).replace(tzinfo=None)
            next_open = FuturesTradingCalendar.next_session_open_after(session_end, batch.instrument_id)
            display_until = (next_open - timedelta(minutes=5)).isoformat()
        except (ValueError, TypeError):
            pass
    run_summaries = [_run_summary(run, session, batch.instrument_id) for run in runs]
    for run_summary in run_summaries:
        run_summary["session_line_state"] = line_states.get(run_summary["run_id"])
    return {
        "batch_id": batch.batch_id, "status": batch.status,
        "instrument": batch.instrument_id, "instrument_name": batch.instrument_name,
        "trading_day": batch.trading_day, "snapshot_time": batch.snapshot_time.isoformat(),
        "quote_time": (snap.get("market", {}).get("quote") or {}).get("timestamp"),
        "snapshot_hash": batch.snapshot_hash, "bar_count": snap["quality"]["bar_count"],
        "analysis_target": snap.get("analysis_target", "TODAY"),
        "analysis_requirements": snap.get("analysis_requirements", ""),
        "use_review_summaries": bool(review_context.get("enabled")),
        "matched_review_summary_count": int(review_context.get("matched_count", 0)),
        "planned_review_summary_count": int(review_context.get("used_count", 0)),
        "used_review_summary_count": len({sid for run in run_summaries for sid in run['used_review_summary_ids']}),
        "used_review_summary_ids": sorted({sid for run in run_summaries for sid in run['used_review_summary_ids']}),
        "next_open_target": snap.get("next_open_target"),
        "future_targets": snap.get("future_targets"),
        "public_materials": snap.get("public_materials"),
        "related_contracts": snap.get("related_contracts", []),
        "related_market_snapshots": snap.get("related_market_snapshots", []),
        "session_target": snap.get("session_target"),
        "session_forecast": snap.get("session_forecast"),
        "display_until": display_until,
        "quality": snap["quality"], "model_count": len(runs),
        "created_at": batch.created_at.isoformat(),
        "completed_at": batch.completed_at.isoformat() if batch.completed_at else None,
        "runs": run_summaries,
    }


@router.get("/analysis/batches/{batch_id}")
def get_batch(batch_id: str, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        batch = session.query(AIAnalysisBatchModel).filter_by(
            batch_id=batch_id, owner_username=user.username).first()
        if not batch:
            raise HTTPException(status_code=404, detail="分析批次不存在")
        return _batch_summary(batch, session)
    finally:
        session.close()


@router.get("/analysis/future/latest")
def latest_future_trends(request: Request, instrument: str = Query(..., min_length=1)):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        rows = session.query(AIAnalysisBatchModel.batch_id, AIAnalysisRunModel.model_config_id).join(
            AIAnalysisRunModel, AIAnalysisRunModel.batch_id == AIAnalysisBatchModel.batch_id,
        ).filter(
            AIAnalysisBatchModel.owner_username == user.username,
            AIAnalysisBatchModel.instrument_id == instrument.strip().upper(),
            func.json_extract(AIAnalysisBatchModel.snapshot_json, '$.analysis_target') == 'FUTURE_TREND',
        ).order_by(AIAnalysisBatchModel.created_at.desc(), AIAnalysisBatchModel.id.desc()).all()
        seen = set()
        batches = {}
        for batch_id, model_id in rows:
            if model_id not in seen:
                seen.add(model_id)
                batches.setdefault(batch_id, set()).add(model_id)
        items = []
        for batch_id, model_ids in batches.items():
            batch = session.query(AIAnalysisBatchModel).filter_by(batch_id=batch_id).one()
            item = _batch_summary(batch, session)
            item['runs'] = [run for run in item['runs'] if run['model_config_id'] in model_ids]
            item['model_count'] = len(item['runs'])
            items.append(item)
        return {'items': items}
    finally:
        session.close()


@router.get("/analysis/history")
def analysis_history(request: Request, instrument: Optional[str] = None,
                     today_only: bool = False,
                     trading_day: Optional[str] = Query(None, pattern=r"^\d{8}$"),
                     page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        query = session.query(AIAnalysisBatchModel).filter_by(owner_username=user.username)
        if instrument:
            query = query.filter_by(instrument_id=instrument.strip().upper())
        current_trading_day = FuturesTradingCalendar.get_trading_day(datetime.now())
        # 盘前展望的报价可能仍属于前一交易日；历史画线按冻结的目标交易日查找。
        history_day = func.coalesce(func.json_unquote(func.json_extract(
            AIAnalysisBatchModel.snapshot_json, '$.session_forecast.trading_day')),
            AIAnalysisBatchModel.trading_day)
        if trading_day:
            query = query.filter(history_day == trading_day)
        elif today_only:
            query = query.filter(history_day == current_trading_day)
        total = query.count()
        rows = query.order_by(AIAnalysisBatchModel.created_at.desc()).offset(
            (page - 1) * page_size).limit(page_size).all()
        return {"items": [_batch_summary(row, session) for row in rows], "total": total,
                "page": page, "page_size": page_size,
                "current_trading_day": current_trading_day, "today_only": today_only}
    finally:
        session.close()


@router.get("/evaluations/stats")
def evaluation_stats(request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        keys = session.query(
            AIAnalysisRunModel.model_config_id,
            AIAnalysisEvaluationModel.analysis_target,
        ).join(
            AIAnalysisEvaluationModel,
            AIAnalysisEvaluationModel.run_id == AIAnalysisRunModel.run_id,
        ).filter(AIAnalysisEvaluationModel.owner_username == user.username).distinct().all()
        models = {}
        for model_config_id, analysis_target in keys:
            models.setdefault(model_config_id, {})[analysis_target] = _model_target_stats(
                session, user.username, model_config_id, analysis_target)
        hit_counts = {}
        batches = session.query(AIAnalysisBatchModel).filter_by(owner_username=user.username).all()
        for batch in batches:
            snap = json.loads(batch.snapshot_json)
            if snap.get("analysis_target") != "CURRENT_SESSION":
                continue
            runs = session.query(AIAnalysisRunModel).filter_by(batch_id=batch.batch_id).all()
            states = settlement.session_line_states(session, batch, runs, snap)
            for run in runs:
                status = (states.get(run.run_id) or {}).get("status")
                if status not in ("HIT", "INVALIDATED"):
                    continue
                counts = hit_counts.setdefault(run.model_config_id, {"hit_count": 0, "miss_count": 0})
                counts["hit_count" if status == "HIT" else "miss_count"] += 1
        for model_id, counts in hit_counts.items():
            total = counts["hit_count"] + counts["miss_count"]
            models.setdefault(model_id, {}).setdefault("CURRENT_SESSION", {})["hit_rate"] = {
                **counts, "decided_count": total,
                "percentage": round(counts["hit_count"] * 100 / total, 1),
            }
        return {"models": models}
    finally:
        session.close()


@router.put("/analysis/runs/{run_id}/evaluation")
def put_evaluation(run_id: str, body: EvaluationBody, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).first()
        batch = session.query(AIAnalysisBatchModel).filter_by(
            batch_id=run.batch_id, owner_username=user.username).first() if run else None
        if not run or not batch:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        if run.status not in {"SUCCEEDED", "UNSTRUCTURED"}:
            raise HTTPException(status_code=409, detail="模型尚未返回可评价结果")
        snap = json.loads(batch.snapshot_json)
        analysis_target = snap.get("analysis_target", "TODAY")
        row = session.query(AIAnalysisEvaluationModel).filter_by(
            owner_username=user.username, run_id=run_id).first()
        now = datetime.utcnow()
        if row is not None:
            # 自动结算的评分允许人工改判一次 (覆盖评级, 保留结算痕迹); 人工评分不可修改或取消
            if (not (row.comment or "").startswith(settlement.AUTO_SETTLE_TAG)
                    or "人工改判" in (row.comment or "")):
                raise HTTPException(status_code=409, detail="评分保存后不可修改或取消")
            suffix = f" → 人工改判为{body.rating}: {body.comment.strip()}" if body.comment.strip() \
                else f" → 人工改判为{body.rating}"
            row.rating = body.rating
            row.score = EVALUATION_SCORES[body.rating]
            row.comment = (row.comment or "") + suffix
            row.updated_at = now
            session.commit()
            session.refresh(row)
            return {
                "evaluation": _evaluation_summary(row),
                "stats": _model_target_stats(
                    session, user.username, run.model_config_id, analysis_target),
                "overridden_auto": True,
            }
        row = AIAnalysisEvaluationModel(
            evaluation_id=str(uuid.uuid4()), run_id=run_id,
            owner_username=user.username, analysis_target=analysis_target,
            rating=body.rating, score=EVALUATION_SCORES[body.rating],
            comment=body.comment.strip() or None, evaluated_at=now, updated_at=now,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return {
            "evaluation": _evaluation_summary(row),
            "stats": _model_target_stats(
                session, user.username, run.model_config_id, analysis_target),
        }
    finally:
        session.close()


@router.delete("/analysis/runs/{run_id}/evaluation")
def delete_evaluation(run_id: str, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).first()
        batch = session.query(AIAnalysisBatchModel).filter_by(
            batch_id=run.batch_id, owner_username=user.username).first() if run else None
        if not run or not batch:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        row = session.query(AIAnalysisEvaluationModel).filter_by(
            owner_username=user.username, run_id=run_id).first()
        if not row:
            raise HTTPException(status_code=404, detail="评分不存在")
        raise HTTPException(status_code=409, detail="评分保存后不可取消")
    finally:
        session.close()


def _create_review_record(owner: str, run_id: str, body: ReviewSummaryBody):
    session = SessionLocal()
    try:
        with _review_gate:
            run, batch = _owned_run(session, owner, run_id)
            if not run or not batch:
                raise HTTPException(status_code=404, detail="模型运行不存在")
            if run.status not in {"SUCCEEDED", "UNSTRUCTURED"}:
                raise HTTPException(status_code=409, detail="原模型尚未返回可复盘结果")
            evaluation = session.query(AIAnalysisEvaluationModel).filter_by(
                owner_username=owner, run_id=run_id).first()
            if not evaluation:
                raise HTTPException(status_code=409, detail="请先完成评分再生成复盘总结")
            guidance = body.user_guidance.strip()
            if evaluation.rating in {"PARTIAL", "WRONG"} and not (guidance or evaluation.comment):
                raise HTTPException(status_code=422, detail="错误或部分正确的评分必须填写复盘依据")
            if session.query(AIAnalysisReviewSummaryModel).filter(
                    AIAnalysisReviewSummaryModel.run_id == run_id,
                    AIAnalysisReviewSummaryModel.status.in_(["PENDING", "RUNNING"])).first():
                raise HTTPException(status_code=409, detail="该回答已有复盘总结正在生成")
            active_reviews = session.query(AIAnalysisReviewSummaryModel.id).filter(
                AIAnalysisReviewSummaryModel.owner_username == owner,
                AIAnalysisReviewSummaryModel.status.in_(["PENDING", "RUNNING"])).count()
            if active_reviews >= MAX_ACTIVE_REVIEWS_PER_USER:
                raise HTTPException(status_code=429, detail="复盘总结任务排队已满，请稍后重试")
            try:
                generator = registry.model_runtime_config(owner, str(body.generator_model_config_id))
            except (ValueError, LookupError, credentials.CredentialError) as exc:
                _raise_config_error(exc)
            snap = json.loads(batch.snapshot_json)
            revision = max((item[0] for item in session.query(
                AIAnalysisReviewSummaryModel.revision_no).filter_by(run_id=run_id).all()), default=0) + 1
            row = AIAnalysisReviewSummaryModel(
                summary_id=str(uuid.uuid4()), run_id=run_id,
                evaluation_id=evaluation.evaluation_id, owner_username=owner,
                source_model_config_id=run.model_config_id,
                generator_model_config_id=generator["model_config_id"],
                generator_config_json=json.dumps(registry.public_snapshot(generator), ensure_ascii=False),
                instrument_id=batch.instrument_id, product_code=extract_product(batch.instrument_id),
                analysis_target=snap.get("analysis_target", "TODAY"),
                session_key=_session_key(snap), scope=body.scope, revision_no=revision,
                user_guidance=guidance or None, status="PENDING", created_at=datetime.utcnow(),
            )
            session.add(row)
            session.commit()
            result = _review_summary(row)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="复盘总结提交冲突，请刷新后重试") from exc
    finally:
        session.close()
    orchestrator.submit_review_summary(result["summary_id"])
    return result


@router.post("/analysis/runs/{run_id}/review-summaries", status_code=202)
def create_review_summary(run_id: str, body: ReviewSummaryBody, request: Request):
    return _create_review_record(_sim_user(request).username, run_id, body)


@router.get("/analysis/runs/{run_id}/review-summaries")
def list_review_summaries(run_id: str, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        run, batch = _owned_run(session, user.username, run_id)
        if not run or not batch:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        rows = session.query(AIAnalysisReviewSummaryModel).filter_by(
            run_id=run_id, owner_username=user.username).order_by(
                AIAnalysisReviewSummaryModel.revision_no.desc()).all()
        return {"items": [_review_summary(row) for row in rows]}
    finally:
        session.close()


@router.get("/review-summaries/{summary_id}")
def get_review_summary(summary_id: str, request: Request, include_raw: bool = False):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        row = session.query(AIAnalysisReviewSummaryModel).filter_by(
            summary_id=summary_id, owner_username=user.username).first()
        if not row:
            raise HTTPException(status_code=404, detail="复盘总结不存在")
        return _review_summary(row, include_raw)
    finally:
        session.close()


@router.post("/review-summaries/{summary_id}/activate")
def activate_review_summary(summary_id: str, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        row = session.query(AIAnalysisReviewSummaryModel).filter_by(
            summary_id=summary_id, owner_username=user.username).first()
        if not row:
            raise HTTPException(status_code=404, detail="复盘总结不存在")
        if row.status not in {"DRAFT", "DISABLED"}:
            raise HTTPException(status_code=409, detail="只有草稿或已停用总结可以启用")
        now = datetime.utcnow()
        session.query(AIAnalysisReviewSummaryModel).filter_by(
            run_id=row.run_id, owner_username=user.username, status="ACTIVE").update({
                "status": "DISABLED", "disabled_at": now,
            }, synchronize_session=False)
        row.status = "ACTIVE"
        row.activated_at = now
        row.disabled_at = None
        session.commit()
        return _review_summary(row)
    finally:
        session.close()


@router.post("/review-summaries/{summary_id}/disable")
def disable_review_summary(summary_id: str, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        row = session.query(AIAnalysisReviewSummaryModel).filter_by(
            summary_id=summary_id, owner_username=user.username).first()
        if not row:
            raise HTTPException(status_code=404, detail="复盘总结不存在")
        if row.status != "ACTIVE":
            raise HTTPException(status_code=409, detail="只有已启用总结可以停用")
        row.status = "DISABLED"
        row.disabled_at = datetime.utcnow()
        session.commit()
        return _review_summary(row)
    finally:
        session.close()


@router.post("/review-summaries/{summary_id}/regenerate", status_code=202)
def regenerate_review_summary(summary_id: str, body: ReviewSummaryBody, request: Request):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        row = session.query(AIAnalysisReviewSummaryModel).filter_by(
            summary_id=summary_id, owner_username=user.username).first()
        if not row:
            raise HTTPException(status_code=404, detail="复盘总结不存在")
        run_id = row.run_id
    finally:
        session.close()
    return _create_review_record(user.username, run_id, body)


def _followup_summary(row: AIAnalysisFollowUpModel, include_raw: bool = False):
    return {
        "followup_id": row.followup_id, "sequence_no": row.sequence_no,
        "question": row.question, "attach_latest": bool(getattr(row, "attach_latest", False)),
        "answer": row.answer, "status": row.status,
        "input_tokens": row.input_tokens, "output_tokens": row.output_tokens,
        "latency_ms": row.latency_ms, "error": row.sanitized_error,
        "created_at": row.created_at.isoformat(),
        "completed_at": row.completed_at.isoformat() if row.completed_at else None,
        **({"request": json.loads(row.request_json) if row.request_json else None,
            "raw_response_text": row.raw_response_text} if include_raw else {}),
    }


def _owned_run(session, owner: str, run_id: str):
    run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).first()
    batch = session.query(AIAnalysisBatchModel).filter_by(
        batch_id=run.batch_id, owner_username=owner).first() if run else None
    return run, batch


@router.get("/analysis/runs/{run_id}/conversation")
def get_conversation(run_id: str, request: Request, include_raw: bool = False):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        run, batch = _owned_run(session, user.username, run_id)
        if not run or not batch:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        rows = session.query(AIAnalysisFollowUpModel).filter_by(
            run_id=run_id, owner_username=user.username).order_by(
                AIAnalysisFollowUpModel.sequence_no.asc()).all()
        try:
            registry.model_runtime_config(user.username, run.model_config_id)
            available, reason = True, None
        except (ValueError, LookupError, credentials.CredentialError) as exc:
            available, reason = False, str(exc)
        return {
            "run_id": run_id, "model_config_id": run.model_config_id,
            "model_display_name": run.model_display_name,
            "available": available, "unavailable_reason": reason,
            "max_turns": 20, "turn_count": len(rows),
            "followups": [_followup_summary(row, include_raw) for row in rows],
        }
    finally:
        session.close()


@router.post("/analysis/runs/{run_id}/follow-ups", status_code=202)
def create_followup(run_id: str, body: FollowUpBody, request: Request):
    user = _sim_user(request)
    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=422, detail="追问内容不能为空")
    session = SessionLocal()
    try:
        with _followup_gate:
            run, batch = _owned_run(session, user.username, run_id)
            if not run or not batch:
                raise HTTPException(status_code=404, detail="模型运行不存在")
            if run.status not in {"SUCCEEDED", "UNSTRUCTURED"}:
                raise HTTPException(status_code=409, detail="原模型尚未返回可追问结果")
            try:
                registry.model_runtime_config(user.username, run.model_config_id)
            except (ValueError, LookupError, credentials.CredentialError) as exc:
                _raise_config_error(exc)
            rows = session.query(AIAnalysisFollowUpModel).filter_by(
                run_id=run_id, owner_username=user.username).all()
            if len(rows) >= 20:
                raise HTTPException(status_code=409, detail="该模型对话已达到20轮上限，请重新发起AI分析")
            if any(row.status in {"PENDING", "RUNNING"} for row in rows):
                raise HTTPException(status_code=409, detail="上一条追问仍在执行")
            active_followups = session.query(AIAnalysisFollowUpModel.id).filter(
                AIAnalysisFollowUpModel.owner_username == user.username,
                AIAnalysisFollowUpModel.status.in_(["PENDING", "RUNNING"])).count()
            if active_followups >= MAX_ACTIVE_FOLLOWUPS_PER_USER:
                raise HTTPException(status_code=429, detail="追问任务排队已满，请等待当前追问完成")
            row = AIAnalysisFollowUpModel(
                followup_id=str(uuid.uuid4()), run_id=run_id, owner_username=user.username,
                sequence_no=max((item.sequence_no for item in rows), default=0) + 1,
                question=question, attach_latest=body.attach_latest_market,
                status="PENDING", created_at=datetime.utcnow(),
            )
            session.add(row)
            session.commit()
            result = _followup_summary(row)
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail="追问提交冲突，请刷新后重试") from exc
    finally:
        session.close()
    orchestrator.submit_followup(result["followup_id"])
    return result


@router.get("/analysis/follow-ups/{followup_id}")
def get_followup(followup_id: str, request: Request, include_raw: bool = False):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        row = session.query(AIAnalysisFollowUpModel).filter_by(
            followup_id=followup_id, owner_username=user.username).first()
        if not row:
            raise HTTPException(status_code=404, detail="追问记录不存在")
        return _followup_summary(row, include_raw)
    finally:
        session.close()


@router.get("/analysis/runs/{run_id}")
def get_run(run_id: str, request: Request, include_raw: bool = False):
    user = _sim_user(request)
    session = SessionLocal()
    try:
        run = session.query(AIAnalysisRunModel).filter_by(run_id=run_id).first()
        if not run:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        batch = session.query(AIAnalysisBatchModel).filter_by(
            batch_id=run.batch_id, owner_username=user.username).first()
        if not batch:
            raise HTTPException(status_code=404, detail="模型运行不存在")
        result = _run_summary(run, session, batch.instrument_id)
        calls = session.query(AIAnalysisCallModel).filter_by(run_id=run_id).order_by(
            AIAnalysisCallModel.sequence_no.asc()).all()
        result["calls"] = [{
            "call_id": call.call_id, "sequence_no": call.sequence_no,
            "call_type": call.call_type, "status": call.status,
            "http_status": call.http_status, "input_tokens": call.input_tokens,
            "output_tokens": call.output_tokens, "latency_ms": call.latency_ms,
            "error": call.sanitized_error,
            **({"request": json.loads(call.request_json),
                "raw_response_text": call.raw_response_text} if include_raw else {}),
        } for call in calls]
        usage = session.query(AIAnalysisReviewUsageModel).filter_by(run_id=run_id).order_by(
            AIAnalysisReviewUsageModel.sequence_no.asc()).all()
        result["review_context"] = [json.loads(item.summary_snapshot_json) for item in usage]
        return result
    finally:
        session.close()
