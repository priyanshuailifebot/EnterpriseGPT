"""Analytics API response models."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field


class DailyExecutionCount(BaseModel):
    date: str
    count: int


class OverviewStatsOut(BaseModel):
    total_executions: int
    successful_executions: int
    failed_executions: int
    avg_duration_ms: float | None
    total_tokens_used: int
    executions_by_day: list[DailyExecutionCount] = Field(default_factory=list)


class RagDocStatOut(BaseModel):
    document_id: str
    title: str
    query_count: int


class ConfidenceBucketOut(BaseModel):
    label: str
    count: int


class RAGStatsOut(BaseModel):
    total_queries: int
    avg_confidence: float | None
    unanswerable_count: int
    top_documents: list[RagDocStatOut] = Field(default_factory=list)
    confidence_buckets: list[ConfidenceBucketOut] = Field(default_factory=list)


class ToolUsageStatOut(BaseModel):
    tool_name: str
    call_count: int
    success_rate: float
    avg_duration_ms: float | None


class ModelCostOut(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class CostStatsOut(BaseModel):
    by_model: list[ModelCostOut] = Field(default_factory=list)
    total_estimated_usd: float


class WorkflowAnalyticsOut(BaseModel):
    workflow_id: UUID
    total_executions: int
    successful_executions: int
    failed_executions: int
    avg_duration_ms: float | None
    total_tokens_used: int
