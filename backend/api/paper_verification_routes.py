"""独立模拟验证 API；只允许登录用户访问自己的研究账本。"""
import uuid
from decimal import Decimal
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field, ConfigDict
from backend.api.auth import require_user
from backend.ai_analysis import paper_verification

router = APIRouter(prefix='/api/v1/ai/verification', tags=['AI模拟验证'])


class CreateVerificationBody(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    instrument: str = Field(min_length=4, max_length=16)
    model_config_id: uuid.UUID
    commission_per_side: Decimal = Field(ge=0, le=1000000)
    slippage_ticks: int = Field(default=0, ge=0, le=100)
    model_input_price_per_million: Decimal = Field(default=Decimal(0), ge=0, le=1000000)
    model_output_price_per_million: Decimal = Field(default=Decimal(0), ge=0, le=1000000)
    analysis_requirements: str = Field(default=paper_verification.REQUIREMENTS, min_length=1, max_length=2000)


def _owner(request):
    user = require_user(request)
    if user.role != 'sim':
        raise HTTPException(status_code=403, detail='仅模拟用户可使用模拟验证')
    return user.username


def _call(action, *args):
    try:
        return action(*args)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get('/plans')
def list_plans(request: Request):
    return {**_call(paper_verification.list_plans, _owner(request)),
            'default_requirements': paper_verification.REQUIREMENTS}


@router.post('/plans', status_code=201)
def create_plan(body: CreateVerificationBody, request: Request):
    return _call(paper_verification.create_plan, _owner(request), body.model_dump(mode='json'))


@router.get('/plans/{plan_id}')
def report(plan_id: uuid.UUID, request: Request):
    return _call(paper_verification.get_report, _owner(request), str(plan_id))


@router.post('/plans/{plan_id}/stop')
def stop(plan_id: uuid.UUID, request: Request):
    return _call(paper_verification.stop_plan, _owner(request), str(plan_id))


class UpdateRequirementsBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    analysis_requirements: str = Field(min_length=1, max_length=2000)


@router.patch('/plans/{plan_id}/requirements')
def update_requirements(plan_id: uuid.UUID, body: UpdateRequirementsBody, request: Request):
    return _call(paper_verification.update_requirements, _owner(request), str(plan_id), body.analysis_requirements)


@router.post('/plans/{plan_id}/resume')
def resume(plan_id: uuid.UUID, request: Request):
    return _call(paper_verification.resume_plan, _owner(request), str(plan_id))


@router.post('/plans/{plan_id}/copy-restart', status_code=201)
def copy_restart(plan_id: uuid.UUID, request: Request):
    return _call(paper_verification.copy_restart_plan, _owner(request), str(plan_id))
