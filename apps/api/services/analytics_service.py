"""Workspace analytics aggregations (SQL-only, async)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import get_settings

# Published list pricing (USD per 1M tokens), approximate — refresh periodically.
_OPENAI_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4-turbo": (Decimal("10.00"), Decimal("30.00")),
    "o1": (Decimal("15.00"), Decimal("60.00")),
    "o1-mini": (Decimal("1.10"), Decimal("4.40")),
    "text-embedding-3-small": (Decimal("0.02"), Decimal("0.00")),
}
_ANTHROPIC_PER_MILLION: dict[str, tuple[Decimal, Decimal]] = {
    "claude-3-5-sonnet-20241022": (Decimal("3.00"), Decimal("15.00")),
    "claude-3-5-sonnet-latest": (Decimal("3.00"), Decimal("15.00")),
    "claude-3-opus-20240229": (Decimal("15.00"), Decimal("75.00")),
    "claude-3-haiku-20240307": (Decimal("0.25"), Decimal("1.25")),
}


def _utc_bounds(start: date | None, end: date | None) -> tuple[datetime, datetime]:
    today = datetime.now(timezone.utc).date()
    s = start or (today - timedelta(days=30))
    e = end or today
    start_dt = datetime(s.year, s.month, s.day, tzinfo=timezone.utc)
    end_dt = datetime(e.year, e.month, e.day, tzinfo=timezone.utc) + timedelta(days=1)
    return start_dt, end_dt


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    key = model.lower().strip()
    for table in (_OPENAI_PER_MILLION, _ANTHROPIC_PER_MILLION):
        if key in table:
            pin, pout = table[key]
            return (Decimal(input_tokens) / Decimal(1_000_000)) * pin + (
                Decimal(output_tokens) / Decimal(1_000_000)
            ) * pout
    for name, (pin, pout) in _OPENAI_PER_MILLION.items():
        if name in key or key in name:
            return (Decimal(input_tokens) / Decimal(1_000_000)) * pin + (
                Decimal(output_tokens) / Decimal(1_000_000)
            ) * pout
    pin, pout = _OPENAI_PER_MILLION["gpt-4o-mini"]
    return (Decimal(input_tokens) / Decimal(1_000_000)) * pin + (
        Decimal(output_tokens) / Decimal(1_000_000)
    ) * pout


@dataclass
class OverviewStats:
    total_executions: int
    successful_executions: int
    failed_executions: int
    avg_duration_ms: float | None
    total_tokens_used: int
    executions_by_day: list[dict[str, Any]]


@dataclass
class RAGStats:
    total_queries: int
    avg_confidence: float | None
    unanswerable_count: int
    top_documents: list[dict[str, Any]]
    confidence_buckets: list[dict[str, Any]]


@dataclass
class ToolUsageStat:
    tool_name: str
    call_count: int
    success_rate: float
    avg_duration_ms: float | None


@dataclass
class CostStats:
    by_model: list[dict[str, Any]]
    total_estimated_usd: float


@dataclass
class WorkflowAnalytics:
    workflow_id: UUID
    total_executions: int
    successful_executions: int
    failed_executions: int
    avg_duration_ms: float | None
    total_tokens_used: int


class AnalyticsService:
    async def get_overview(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        start: date | None,
        end: date | None,
    ) -> OverviewStats:
        start_dt, end_dt = _utc_bounds(start, end)
        base = text(
            """
            SELECT
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE we.status IN ('completed'))::int AS ok,
                COUNT(*) FILTER (WHERE we.status IN ('failed', 'cancelled'))::int AS bad,
                AVG(we.duration_ms)::float AS avg_ms,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(trim(we.output_data->>'total_tokens'), '')::int,
                        NULLIF(trim(we.output_data #>> '{usage,total_tokens}'), '')::int,
                        0
                    )
                ), 0)::bigint AS tokens
            FROM workflow_executions we
            INNER JOIN workflows w ON w.id = we.workflow_id
            WHERE w.workspace_id = :ws
              AND we.started_at >= :start
              AND we.started_at < :end
            """
        )
        row = (await db.execute(base, {"ws": workspace_id, "start": start_dt, "end": end_dt})).mappings().first()
        total = int(row["total"] or 0) if row else 0
        ok = int(row["ok"] or 0) if row else 0
        bad = int(row["bad"] or 0) if row else 0
        avg_ms = float(row["avg_ms"]) if row and row["avg_ms"] is not None else None
        tokens = int(row["tokens"] or 0) if row else 0

        series_q = text(
            """
            SELECT
                (date_trunc('day', we.started_at AT TIME ZONE 'UTC'))::date AS d,
                COUNT(*)::int AS c
            FROM workflow_executions we
            INNER JOIN workflows w ON w.id = we.workflow_id
            WHERE w.workspace_id = :ws
              AND we.started_at >= :start
              AND we.started_at < :end
            GROUP BY 1
            ORDER BY 1 ASC
            """
        )
        series_rows = (await db.execute(series_q, {"ws": workspace_id, "start": start_dt, "end": end_dt})).mappings().all()
        by_day = [{"date": str(r["d"]), "count": int(r["c"])} for r in series_rows]

        return OverviewStats(
            total_executions=total,
            successful_executions=ok,
            failed_executions=bad,
            avg_duration_ms=avg_ms,
            total_tokens_used=tokens,
            executions_by_day=by_day,
        )

    async def get_rag_stats(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        start: date | None,
        end: date | None,
    ) -> RAGStats:
        start_dt, end_dt = _utc_bounds(start, end)
        q = text(
            """
            SELECT
                COUNT(*)::int AS n,
                AVG(r.confidence)::float AS avg_conf,
                COUNT(*) FILTER (WHERE r.unanswerable)::int AS unans
            FROM rag_query_logs r
            WHERE r.workspace_id = :ws
              AND r.created_at >= :start
              AND r.created_at < :end
            """
        )
        row = (await db.execute(q, {"ws": workspace_id, "start": start_dt, "end": end_dt})).mappings().first()
        n = int(row["n"] or 0) if row else 0
        avg_c = float(row["avg_conf"]) if row and row["avg_conf"] is not None else None
        unans = int(row["unans"] or 0) if row else 0

        top_q = text(
            """
            SELECT
                r.top_document_id AS doc_id,
                d.filename AS title,
                COUNT(*)::int AS c
            FROM rag_query_logs r
            LEFT JOIN documents d ON d.id = r.top_document_id
            WHERE r.workspace_id = :ws
              AND r.created_at >= :start
              AND r.created_at < :end
              AND r.top_document_id IS NOT NULL
            GROUP BY r.top_document_id, d.filename
            ORDER BY c DESC
            LIMIT 10
            """
        )
        tops = (await db.execute(top_q, {"ws": workspace_id, "start": start_dt, "end": end_dt})).mappings().all()
        top_docs = [
            {
                "document_id": str(t["doc_id"]),
                "title": t["title"] or "unknown",
                "query_count": int(t["c"]),
            }
            for t in tops
        ]

        bucket_q = text(
            """
            SELECT (LEAST(9, GREATEST(0, floor(confidence * 10)::int)))::int AS b, COUNT(*)::int AS c
            FROM rag_query_logs r
            WHERE r.workspace_id = :ws
              AND r.created_at >= :start
              AND r.created_at < :end
            GROUP BY 1
            ORDER BY 1
            """
        )
        br = (await db.execute(bucket_q, {"ws": workspace_id, "start": start_dt, "end": end_dt})).mappings().all()
        bucket_map = {int(x["b"]): int(x["c"]) for x in br}
        buckets: list[dict[str, Any]] = []
        for b in range(10):
            lo = b / 10
            hi = (b + 1) / 10
            buckets.append(
                {
                    "label": f"{lo:.1f}-{hi:.1f}",
                    "count": bucket_map.get(b, 0),
                }
            )

        return RAGStats(
            total_queries=n,
            avg_confidence=avg_c,
            unanswerable_count=unans,
            top_documents=top_docs,
            confidence_buckets=buckets,
        )

    async def get_tool_usage(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
    ) -> list[ToolUsageStat]:
        q = text(
            """
            SELECT
                tel.tool_name AS tool_name,
                COUNT(*)::int AS calls,
                AVG(CASE WHEN tel.success THEN 1.0 ELSE 0.0 END)::float AS rate,
                AVG(tel.duration_ms)::float AS avg_ms
            FROM tool_execution_logs tel
            INNER JOIN workflow_executions we ON we.id = tel.execution_id
            INNER JOIN workflows w ON w.id = we.workflow_id
            WHERE w.workspace_id = :ws
              AND tel.execution_id IS NOT NULL
            GROUP BY tel.tool_name
            ORDER BY calls DESC
            LIMIT 100
            """
        )
        rows = (await db.execute(q, {"ws": workspace_id})).mappings().all()
        return [
            ToolUsageStat(
                tool_name=str(r["tool_name"]),
                call_count=int(r["calls"]),
                success_rate=float(r["rate"] or 0),
                avg_duration_ms=float(r["avg_ms"]) if r["avg_ms"] is not None else None,
            )
            for r in rows
        ]

    async def get_cost_estimate(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        start: date | None,
        end: date | None,
    ) -> CostStats:
        start_dt, end_dt = _utc_bounds(start, end)
        settings = get_settings()
        default_model = settings.AZURE_OPENAI_DEFAULT_MODEL or "gpt-4o-mini"
        q = text(
            """
            SELECT
                COALESCE(
                    NULLIF(trim(we.output_data->>'model'), ''),
                    NULLIF(trim(we.output_data #>> '{usage,model}'), ''),
                    :default_model
                ) AS model,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(trim(we.output_data #>> '{usage,prompt_tokens}'), '')::bigint,
                        NULLIF(trim(we.output_data #>> '{usage,input_tokens}'), '')::bigint,
                        0
                    )
                ), 0)::bigint AS in_tok,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(trim(we.output_data #>> '{usage,completion_tokens}'), '')::bigint,
                        NULLIF(trim(we.output_data #>> '{usage,output_tokens}'), '')::bigint,
                        0
                    )
                ), 0)::bigint AS out_tok
            FROM workflow_executions we
            INNER JOIN workflows w ON w.id = we.workflow_id
            WHERE w.workspace_id = :ws
              AND we.started_at >= :start
              AND we.started_at < :end
            GROUP BY 1
            """
        )
        rows = (
            await db.execute(
                q,
                {
                    "ws": workspace_id,
                    "start": start_dt,
                    "end": end_dt,
                    "default_model": default_model,
                },
            )
        ).mappings().all()

        by_model: list[dict[str, Any]] = []
        total_usd = Decimal("0")
        for r in rows:
            model = str(r["model"] or default_model)
            it = int(r["in_tok"] or 0)
            ot = int(r["out_tok"] or 0)
            if it == 0 and ot == 0:
                continue
            cost = _estimate_cost_usd(model, it, ot)
            total_usd += cost
            by_model.append(
                {
                    "model": model,
                    "input_tokens": it,
                    "output_tokens": ot,
                    "estimated_cost_usd": float(cost),
                }
            )

        return CostStats(by_model=by_model, total_estimated_usd=float(total_usd))

    async def get_workflow_stats(
        self,
        db: AsyncSession,
        *,
        workspace_id: UUID,
        workflow_id: UUID,
        start: date | None,
        end: date | None,
    ) -> WorkflowAnalytics | None:
        wf_check = text(
            "SELECT id FROM workflows WHERE id = :id AND workspace_id = :ws LIMIT 1"
        )
        exists = (await db.execute(wf_check, {"id": workflow_id, "ws": workspace_id})).first()
        if exists is None:
            return None
        start_dt, end_dt = _utc_bounds(start, end)
        q = text(
            """
            SELECT
                COUNT(*)::int AS total,
                COUNT(*) FILTER (WHERE we.status IN ('completed'))::int AS ok,
                COUNT(*) FILTER (WHERE we.status IN ('failed', 'cancelled'))::int AS bad,
                AVG(we.duration_ms)::float AS avg_ms,
                COALESCE(SUM(
                    COALESCE(
                        NULLIF(trim(we.output_data->>'total_tokens'), '')::int,
                        NULLIF(trim(we.output_data #>> '{usage,total_tokens}'), '')::int,
                        0
                    )
                ), 0)::bigint AS tokens
            FROM workflow_executions we
            WHERE we.workflow_id = :wf
              AND we.started_at >= :start
              AND we.started_at < :end
            """
        )
        row = (await db.execute(q, {"wf": workflow_id, "start": start_dt, "end": end_dt})).mappings().first()
        if not row:
            return WorkflowAnalytics(
                workflow_id=workflow_id,
                total_executions=0,
                successful_executions=0,
                failed_executions=0,
                avg_duration_ms=None,
                total_tokens_used=0,
            )
        return WorkflowAnalytics(
            workflow_id=workflow_id,
            total_executions=int(row["total"] or 0),
            successful_executions=int(row["ok"] or 0),
            failed_executions=int(row["bad"] or 0),
            avg_duration_ms=float(row["avg_ms"]) if row["avg_ms"] is not None else None,
            total_tokens_used=int(row["tokens"] or 0),
        )
