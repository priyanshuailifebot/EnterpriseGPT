"""Workspace analytics HTTP API."""

from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from core.permissions import Permission, require_permission
from core.security import get_current_active_user
from models.user import User
from schemas.analytics import (
    ConfidenceBucketOut,
    CostStatsOut,
    DailyExecutionCount,
    ModelCostOut,
    OverviewStatsOut,
    RAGStatsOut,
    RagDocStatOut,
    ToolUsageStatOut,
    WorkflowAnalyticsOut,
)
from services.analytics_service import AnalyticsService
from services.workflow_service import ensure_workspace_membership

router = APIRouter(prefix="/analytics", tags=["analytics"])


def get_analytics_service() -> AnalyticsService:
    return AnalyticsService()


@router.get(
    "/overview",
    response_model=OverviewStatsOut,
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def analytics_overview(
    workspace_id: UUID = Query(...),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> OverviewStatsOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    o = await svc.get_overview(db, workspace_id=workspace_id, start=start, end=end)
    return OverviewStatsOut(
        total_executions=o.total_executions,
        successful_executions=o.successful_executions,
        failed_executions=o.failed_executions,
        avg_duration_ms=o.avg_duration_ms,
        total_tokens_used=o.total_tokens_used,
        executions_by_day=[DailyExecutionCount(**x) for x in o.executions_by_day],
    )


@router.get(
    "/rag",
    response_model=RAGStatsOut,
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def analytics_rag(
    workspace_id: UUID = Query(...),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> RAGStatsOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    r = await svc.get_rag_stats(db, workspace_id=workspace_id, start=start, end=end)
    return RAGStatsOut(
        total_queries=r.total_queries,
        avg_confidence=r.avg_confidence,
        unanswerable_count=r.unanswerable_count,
        top_documents=[RagDocStatOut(**d) for d in r.top_documents],
        confidence_buckets=[ConfidenceBucketOut(**d) for d in r.confidence_buckets],
    )


@router.get(
    "/tools",
    response_model=list[ToolUsageStatOut],
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def analytics_tools(
    workspace_id: UUID = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> list[ToolUsageStatOut]:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    rows = await svc.get_tool_usage(db, workspace_id=workspace_id)
    return [
        ToolUsageStatOut(
            tool_name=t.tool_name,
            call_count=t.call_count,
            success_rate=t.success_rate,
            avg_duration_ms=t.avg_duration_ms,
        )
        for t in rows
    ]


@router.get(
    "/costs",
    response_model=CostStatsOut,
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def analytics_costs(
    workspace_id: UUID = Query(...),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> CostStatsOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    c = await svc.get_cost_estimate(db, workspace_id=workspace_id, start=start, end=end)
    return CostStatsOut(
        by_model=[ModelCostOut(**m) for m in c.by_model],
        total_estimated_usd=c.total_estimated_usd,
    )


@router.get(
    "/workflows/{workflow_id}",
    response_model=WorkflowAnalyticsOut,
    dependencies=[require_permission(Permission.ANALYTICS_READ)],
)
async def analytics_workflow_detail(
    workflow_id: UUID,
    workspace_id: UUID = Query(...),
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_active_user),
    svc: AnalyticsService = Depends(get_analytics_service),
) -> WorkflowAnalyticsOut:
    await ensure_workspace_membership(db, user_id=user.id, workspace_id=workspace_id)
    w = await svc.get_workflow_stats(
        db, workspace_id=workspace_id, workflow_id=workflow_id, start=start, end=end
    )
    if w is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return WorkflowAnalyticsOut(
        workflow_id=w.workflow_id,
        total_executions=w.total_executions,
        successful_executions=w.successful_executions,
        failed_executions=w.failed_executions,
        avg_duration_ms=w.avg_duration_ms,
        total_tokens_used=w.total_tokens_used,
    )
