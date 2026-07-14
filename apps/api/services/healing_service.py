"""Self-healing pipeline — Phase 1 (evidence + diagnosis).

Implements the first two nodes of the Doctor pipeline described in
``docs/SELF_HEALING_PLAN.md``:

* ``gather_evidence`` — assembles a typed :class:`EvidenceDigest` from a
  workflow's recent runs. Handles the three evidence realities the audit
  surfaced: mixed-kind (extended) runs carry per-node steps; agent-only
  (Dynamiq) runs carry none, so we fall back to execution-level signal; chat
  workflows emit no ``WorkflowExecution`` rows at all, so we read
  ``chat_sessions`` / ``chat_messages``; and a workflow with no history gets a
  manufactured demo run.
* ``diagnose`` — one structured LLM call (the same Azure JSON-mode path the NL
  interpreter uses) turning the digest + definition into a
  :class:`HealingReport` of typed :class:`Finding` objects.

Authorization is intentionally NOT enforced here — the service is called both
from the interactive endpoint (user-scoped) and, later, from the headless
monitor (no request user). Callers gate access (audit finding J).
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agents.healing import prompts as heal_prompts
from agents.healing.sanitizer import sanitize_definition
from core.config import Settings
from core.redis import get_redis
from models.chat_session import ChatMessage, ChatSession
from models.user import User
from models.workflow import Workflow, WorkflowStatus
from models.workflow_execution import WorkflowExecution
from models.workflow_execution_step import WorkflowExecutionStep
from models.workflow_version import WorkflowVersion
from models.workspace_member import WorkspaceMember
from schemas.healing import (
    EvidenceDigest,
    Finding,
    HealingReport,
    PatchResult,
    RunEvidence,
    StepEvidence,
    VerifyResult,
)
from schemas.workflow import WorkflowDefinition, WorkflowUpdateBody
from services.workflow_interpreter import (
    WorkflowInterpretationError,
    WorkflowInterpreter,
    diff_definitions,
)
from services.workflow_requirements import derive_requirements

log = structlog.get_logger(__name__)

_OUTPUT_PREVIEW_CHARS = 300
_DEFINITION_JSON_CHARS = 20000

# The heal-level repair loop that wraps augment. augment has its own 1-pass
# JSON/schema retry; this loop re-prompts when the sanitized, schema-valid patch
# still edits nodes outside the finding's blast radius (audit finding G).
MAX_PATCH_REPAIRS = 2

# Incident audit trail in Redis (mirrors Dograh's capped list).
_INCIDENT_KEY = "egpt:heal:incidents:{workflow_id}"
_INCIDENT_CAP = 50
_VERIFY_MAX_TURNS = 14
_TRANSCRIPT_CHARS = 6000

# Headless coordination keys.
_LOCK_KEY = "egpt:heal:lock:{workflow_id}"
_LOCK_TTL_SECONDS = 900
_COOLDOWN_KEY = "egpt:heal:cooldown:{workflow_id}"

_POLICY_RANK = {"off": 0, "safe": 1, "autonomous": 2}


class HealingError(RuntimeError):
    """Raised when a heal cannot proceed (missing workflow, no LLM creds, …)."""


class HealingService:
    """Diagnose (and, in later phases, patch/validate/verify/apply) workflows."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._wf_cache: Any = None  # lazily-built WorkflowService for headless writes

    # ------------------------------------------------------------------ #
    # Evidence
    # ------------------------------------------------------------------ #
    async def gather_evidence(
        self,
        db: AsyncSession,
        workflow_id: uuid.UUID,
        *,
        max_runs: int = 5,
        manufacture: bool = True,
    ) -> EvidenceDigest:
        """Assemble the evidence digest for one workflow.

        ``manufacture=True`` runs a demo when no run history exists so healing
        works on a freshly built workflow ("I just built this, check it").
        """
        workflow, definition = await self._load_workflow(db, workflow_id)
        trigger_type = _detect_trigger_type(definition)

        digest = EvidenceDigest(
            workflow_id=str(workflow_id),
            workflow_name=workflow.name,
            trigger_type=trigger_type,
        )

        # 1. Prefer real (non-demo) execution history.
        real_rows = await self._recent_executions(db, workflow_id, demo=False, limit=max_runs)
        if real_rows:
            digest.runs = await self._runs_from_rows(db, real_rows)
            digest.source = "real_runs"
            digest.steps_available = any(r.steps for r in digest.runs)
            return digest

        # 2. Chat workflows keep their history in chat_sessions / chat_messages,
        #    not WorkflowExecution — read that instead.
        if trigger_type == "chat":
            chat_notes = await self._chat_evidence(db, workflow_id, limit=max_runs)
            if chat_notes:
                digest.chat_notes = chat_notes
                digest.source = "chat"
                return digest

        # 3. Fall back to any prior demo runs before spending an LLM call.
        demo_rows = await self._recent_executions(db, workflow_id, demo=True, limit=max_runs)
        if demo_rows:
            digest.runs = await self._runs_from_rows(db, demo_rows)
            digest.source = "manufactured"
            digest.steps_available = any(r.steps for r in digest.runs)
            digest.notes = "No real runs; using existing demo run history."
            return digest

        # 4. Nothing on record — manufacture a demo run.
        if manufacture:
            run = await self._manufacture_demo_run(definition, workflow.workspace_id)
            if run is not None:
                digest.runs = [run]
                digest.source = "manufactured"
                digest.steps_available = bool(run.steps)
                digest.notes = (
                    "No run history existed; evidence is from a freshly executed "
                    "demo run (structural smoke test — a demo run always completes)."
                )
                return digest

        digest.notes = "No run history and no evidence could be manufactured."
        return digest

    async def _recent_executions(
        self, db: AsyncSession, workflow_id: uuid.UUID, *, demo: bool, limit: int
    ) -> list[WorkflowExecution]:
        stmt = (
            select(WorkflowExecution)
            .where(
                WorkflowExecution.workflow_id == workflow_id,
                WorkflowExecution.demo.is_(demo),
            )
            .order_by(WorkflowExecution.started_at.desc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())

    async def _runs_from_rows(
        self, db: AsyncSession, rows: list[WorkflowExecution]
    ) -> list[RunEvidence]:
        runs: list[RunEvidence] = []
        for row in rows:
            steps_stmt = (
                select(WorkflowExecutionStep)
                .where(WorkflowExecutionStep.execution_id == row.id)
                .order_by(WorkflowExecutionStep.step_index.asc())
            )
            step_rows = list((await db.execute(steps_stmt)).scalars().all())
            runs.append(
                RunEvidence(
                    execution_id=str(row.id),
                    status=_enum_value(row.status),
                    demo=bool(row.demo),
                    error_message=row.error_message,
                    duration_ms=row.duration_ms,
                    started_at=row.started_at.isoformat() if row.started_at else None,
                    steps=[_step_evidence(s) for s in step_rows],
                )
            )
        return runs

    async def _chat_evidence(
        self, db: AsyncSession, workflow_id: uuid.UUID, *, limit: int
    ) -> str:
        sessions = list(
            (
                await db.execute(
                    select(ChatSession)
                    .where(ChatSession.workflow_id == workflow_id)
                    .order_by(ChatSession.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not sessions:
            return ""

        total_messages = sum(int(s.total_messages or 0) for s in sessions)
        lines = [
            f"{len(sessions)} recent chat session(s); {total_messages} message(s) total."
        ]

        session_ids = [s.id for s in sessions]
        parser_errors = list(
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id.in_(session_ids),
                        ChatMessage.parser_error.isnot(None),
                    )
                    .order_by(ChatMessage.created_at.desc())
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        if parser_errors:
            lines.append(f"{len(parser_errors)} message(s) with output-parser errors:")
            for msg in parser_errors[:10]:
                lines.append(f"  - {(msg.parser_error or '')[:300]}")
        return "\n".join(lines)

    async def _manufacture_demo_run(
        self, definition: WorkflowDefinition, workspace_id: uuid.UUID
    ) -> RunEvidence | None:
        # Imported lazily to avoid any import-time coupling with the executor.
        from services.demo_executor import run_demo

        steps: list[StepEvidence] = []
        final_status = "completed"
        error_message: str | None = None
        try:
            async for ev in run_demo(
                definition=definition,
                settings=self._settings,
                workspace_id=workspace_id,
            ):
                etype = ev.get("type")
                # ``node_complete`` fires once for every executed node (agents,
                # merges, and all other kinds) and carries the node's kind +
                # output_snapshot. Agent/merge nodes ALSO emit ``agent_complete``
                # just before it, so matching only ``node_complete`` avoids the
                # duplicate, mislabeled rows that matching both would produce.
                if etype == "node_complete":
                    steps.append(_step_evidence_from_event(ev))
                elif etype == "error":
                    final_status = "failed"
                    error_message = _event_text(ev) or "demo run emitted an error event"
        except Exception as exc:  # noqa: BLE001 — a broken def must not crash diagnosis
            log.warning("healing.manufacture_demo_failed", error=str(exc))
            final_status = "failed"
            error_message = f"demo run raised: {exc}"

        return RunEvidence(
            execution_id="demo-manufactured",
            status=final_status,
            demo=True,
            error_message=error_message,
            steps=steps,
        )

    # ------------------------------------------------------------------ #
    # Diagnosis
    # ------------------------------------------------------------------ #
    async def diagnose(
        self,
        db: AsyncSession,
        workflow_id: uuid.UUID,
        digest: EvidenceDigest,
        *,
        complaint: str | None = None,
        triggered_by: str = "chat",
    ) -> HealingReport:
        """Turn the evidence digest into a structured :class:`HealingReport`."""
        workflow, definition = await self._load_workflow(db, workflow_id)

        definition_json = json.dumps(definition.model_dump(), indent=2)[:_DEFINITION_JSON_CHARS]
        user_content = (
            f"COMPLAINT / TRIGGER: {complaint or '(autonomous health check)'}\n\n"
            f"EVIDENCE DIGEST:\n{digest.as_text()}\n\n"
            f"WORKFLOW DEFINITION (JSON):\n{definition_json}"
        )
        messages = [
            {"role": "system", "content": heal_prompts.DOCTOR_DIAGNOSE_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        raw = await self._call_llm(messages=messages)
        data = _parse_json_object(raw)

        findings = _parse_findings(data.get("findings"))
        health = data.get("health", "unknown")
        if health not in heal_prompts.VALID_HEALTH:
            health = "unknown"

        return HealingReport(
            incident_id=uuid.uuid4().hex,
            workflow_id=str(workflow_id),
            workspace_id=str(workflow.workspace_id),
            health=health,
            summary=str(data.get("summary", "") or "")[:2000],
            findings=findings,
            evidence_source=digest.source,
            triggered_by=triggered_by if triggered_by in ("chat", "api", "monitor") else "chat",
        )

    # ------------------------------------------------------------------ #
    # Patch (augment + sanitize + scope guard + repair loop)
    # ------------------------------------------------------------------ #
    async def patch(
        self,
        db: AsyncSession,
        workflow_id: uuid.UUID,
        report: HealingReport,
        *,
        selected_finding_ids: list[str] | None = None,
    ) -> PatchResult:
        """Generate a patched definition for the report's auto-fixable findings.

        Reuses ``WorkflowInterpreter.augment`` (which returns a schema-valid
        definition), then runs the deterministic sanitizer, a scope guard, and
        a bounded repair loop. Never writes anything.
        """
        _workflow, definition = await self._load_workflow(db, workflow_id)

        fixable = [
            f
            for f in report.findings
            if f.auto_fixable
            and (selected_finding_ids is None or f.finding_id in selected_finding_ids)
        ]
        if not fixable:
            return PatchResult(patched=None)

        allowed_ids = _allowed_edit_ids(definition, fixable)
        instruction = _patch_instruction(fixable)
        available_tools = _available_tools(definition)
        interp = WorkflowInterpreter(self._settings)

        scope_note = ""
        last: PatchResult | None = None
        for attempt in range(MAX_PATCH_REPAIRS + 1):
            try:
                raw = await interp.augment(
                    current_definition=definition,
                    user_message=instruction + scope_note,
                    available_tools=available_tools,
                )
                cleaned_dict, sani_notes = sanitize_definition(raw.model_dump())
                patched = WorkflowDefinition.model_validate(cleaned_dict)
            except ValidationError as exc:
                # augment output + sanitizer diverged into an invalid graph:
                # feed the error back as a repair attempt instead of crashing
                # (matches the sanitizer's documented repair-loop contract).
                scope_note = (
                    "\n\nYour previous output failed schema validation:\n"
                    f"{exc}\nRe-emit the COMPLETE corrected definition."
                )
                log.info("healing.patch_validation_retry", attempt=attempt)
                continue

            violations = _scope_violations(before=definition, after=patched, allowed=allowed_ids)
            required = sorted(
                {s.provider for s in derive_requirements(patched) if s.required}
            )
            last = PatchResult(
                patched=patched,
                changes=diff_definitions(before=definition, after=patched),
                scope_warnings=violations,
                sanitizer_notes=sani_notes,
                required_providers=required,
                repairs=attempt,
            )
            if not violations:
                return last
            scope_note = (
                "\n\nSCOPE VIOLATION — you changed nodes outside the allowed set "
                f"{sorted(allowed_ids)}: {violations}. Re-emit the COMPLETE definition "
                "changing ONLY the allowed nodes and copying every other node verbatim."
            )
            log.info("healing.patch_scope_retry", attempt=attempt, violations=violations)

        # Repairs exhausted. For an interactive heal we still surface the patch
        # (the human judges it at the propose gate); the warnings ride along.
        return last or PatchResult(patched=None)

    # ------------------------------------------------------------------ #
    # Verify (demo-mode run + judge)
    # ------------------------------------------------------------------ #
    async def verify(
        self, definition: WorkflowDefinition, workspace_id: uuid.UUID
    ) -> VerifyResult:
        """Run the patched definition in demo mode and judge the transcript.

        A demo run always completes, so ``reached_end`` is not the gate — the
        LLM judge verdict is (audit finding B).
        """
        from services.demo_executor import run_demo

        node_path: list[str] = []
        transcript_parts: list[str] = []
        reached_end = False
        error: str | None = None
        try:
            async for ev in run_demo(
                definition=definition,
                settings=self._settings,
                workspace_id=workspace_id,
            ):
                etype = ev.get("type")
                if etype == "node_complete":
                    node_path.append(str(ev.get("node_id") or ""))
                    out = _preview(ev.get("output_snapshot"))
                    if out:
                        transcript_parts.append(f"{ev.get('node_id')}: {out}")
                elif etype == "agent_complete":
                    text = _event_text(ev)
                    if text:
                        transcript_parts.append(f"{ev.get('agent_id')}: {text}")
                elif etype == "workflow_complete":
                    reached_end = True
                elif etype == "error":
                    error = _event_text(ev) or "demo run emitted an error event"
        except Exception as exc:  # noqa: BLE001 — a broken def must not crash the heal
            log.warning("healing.verify_demo_failed", error=str(exc))
            return VerifyResult(ran=True, error=f"demo run raised: {exc}", verdict="fail",
                                reason="the demo run raised an exception")

        transcript = "\n".join(transcript_parts)[:_TRANSCRIPT_CHARS]
        verdict, reason = await self._judge(transcript)
        return VerifyResult(
            ran=True,
            reached_end=reached_end,
            error=error,
            node_path=node_path,
            verdict=verdict,
            reason=reason,
        )

    async def _judge(self, transcript: str) -> tuple[str, str]:
        if not transcript.strip():
            return "unknown", "no transcript was produced by the demo run"
        try:
            raw = await self._call_llm(
                messages=[
                    {"role": "system", "content": heal_prompts.DOCTOR_JUDGE_SYSTEM},
                    {"role": "user", "content": f"TRANSCRIPT:\n{transcript}"},
                ]
            )
            data = _parse_json_object(raw)
        except HealingError as exc:
            return "unknown", f"judge call failed: {exc}"
        verdict = data.get("verdict")
        if verdict not in ("pass", "warn", "fail"):
            verdict = "unknown"
        return verdict, str(data.get("reason", ""))[:1000]

    # ------------------------------------------------------------------ #
    # Incident audit trail
    # ------------------------------------------------------------------ #
    async def save_incident(self, report: HealingReport) -> None:
        try:
            redis = get_redis()
            key = _INCIDENT_KEY.format(workflow_id=report.workflow_id)
            await redis.lpush(key, json.dumps(report.model_dump(), default=str))
            await redis.ltrim(key, 0, _INCIDENT_CAP - 1)
        except Exception as exc:  # noqa: BLE001 — audit must not break the heal
            log.warning("healing.save_incident_failed", error=str(exc))

    async def list_incidents(self, workflow_id: uuid.UUID, *, limit: int = 20) -> list[dict[str, Any]]:
        try:
            redis = get_redis()
            key = _INCIDENT_KEY.format(workflow_id=str(workflow_id))
            rows = await redis.lrange(key, 0, max(limit - 1, 0))
        except Exception as exc:  # noqa: BLE001
            log.warning("healing.list_incidents_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for raw in rows:
            try:
                out.append(json.loads(raw.decode() if isinstance(raw, bytes) else raw))
            except (ValueError, AttributeError):
                continue
        return out

    # ------------------------------------------------------------------ #
    # Orchestrator — streams SSE events, STOPS at the propose gate.
    # ------------------------------------------------------------------ #
    async def heal(
        self,
        db: AsyncSession,
        workflow_id: uuid.UUID,
        *,
        complaint: str | None = None,
        triggered_by: str = "chat",
        selected_finding_ids: list[str] | None = None,
        simulate: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Run gather_evidence → diagnose → patch (→ verify) and propose.

        This never writes. The final ``propose`` event carries the validated
        patched definition for a human to accept in the editor; applying it is a
        separate, explicit save (and, for the autonomous tier, Phase 3).
        """
        yield {"type": "heal_start", "workflow_id": str(workflow_id)}

        try:
            digest = await self.gather_evidence(db, workflow_id)
        except HealingError as exc:
            yield {"type": "error", "content": str(exc)}
            return
        yield {"type": "evidence", "source": digest.source, "runs": len(digest.runs)}

        report = await self.diagnose(
            db, workflow_id, digest, complaint=complaint, triggered_by=triggered_by
        )
        yield {"type": "diagnosis", "report": report.model_dump()}

        fixable = [
            f
            for f in report.fixable_findings
            if selected_finding_ids is None or f.finding_id in selected_finding_ids
        ]
        if not fixable:
            report.patches_proposed = [
                f.proposed_fix for f in report.findings if not f.auto_fixable
            ]
            await self.save_incident(report)
            yield {"type": "healing_report", "report": report.model_dump()}
            return

        try:
            patch_result = await self.patch(
                db, workflow_id, report, selected_finding_ids=selected_finding_ids
            )
        except WorkflowInterpretationError as exc:
            yield {"type": "patch_failed", "content": str(exc)}
            await self.save_incident(report)
            yield {"type": "healing_report", "report": report.model_dump()}
            return

        if patch_result.patched is None:
            await self.save_incident(report)
            yield {"type": "healing_report", "report": report.model_dump()}
            return

        report.patches_applied = patch_result.changes
        report.validation_passed = True
        yield {
            "type": "validation",
            "valid": True,
            "changes": patch_result.changes,
            "scope_warnings": patch_result.scope_warnings,
            "sanitizer_notes": patch_result.sanitizer_notes,
            "required_providers": patch_result.required_providers,
        }

        if simulate:
            workflow, _ = await self._load_workflow(db, workflow_id)
            verify_result = await self.verify(patch_result.patched, workflow.workspace_id)
            report.simulation_verdict = f"{verify_result.verdict}: {verify_result.reason}"
            yield {"type": "verification", **verify_result.model_dump()}

        report.patches_proposed = [
            f.proposed_fix for f in report.findings if not f.auto_fixable
        ]
        await self.save_incident(report)

        # PROPOSE gate — surface the validated patch and STOP. No write.
        yield {
            "type": "propose",
            "report": report.model_dump(),
            "proposed_definition": patch_result.patched.model_dump(),
            "changes": patch_result.changes,
            "scope_warnings": patch_result.scope_warnings,
            "required_providers": patch_result.required_providers,
        }

    # ------------------------------------------------------------------ #
    # Headless self-heal (ARQ monitor path) — no human in the loop.
    # ------------------------------------------------------------------ #
    def effective_policy(self, workflow: Workflow, env_ceiling: str) -> str:
        """Resolve the policy for a workflow: its own policy, capped by the env
        ceiling (a workflow set to ``autonomous`` still only drafts if the env
        is ``safe``). ``off`` if the workflow hasn't opted in."""
        cfg = workflow.self_heal or {}
        if not cfg.get("enabled"):
            return "off"
        wf_policy = str(cfg.get("policy", "safe"))
        if wf_policy not in _POLICY_RANK:
            wf_policy = "safe"
        return min(wf_policy, env_ceiling, key=lambda p: _POLICY_RANK.get(p, 0))

    async def heal_headless(
        self,
        db: AsyncSession,
        workflow_id: uuid.UUID,
        *,
        complaint: str | None = None,
        policy: str = "safe",
        triggered_by: str = "monitor",
    ) -> HealingReport:
        """Run the full pipeline with no human gate, applying per ``policy``.

        Never raises — a failed heal is recorded on the report and the incident
        so one bad workflow can't break a monitor pass.
        """
        workflow, _definition = await self._load_workflow(db, workflow_id)
        base_version = workflow.current_version
        owner = await self._resolve_owner(db, workflow)

        digest = await self.gather_evidence(db, workflow_id)
        report = await self.diagnose(
            db, workflow_id, digest, complaint=complaint, triggered_by=triggered_by
        )
        report.patches_proposed = [
            f.proposed_fix for f in report.findings if not f.auto_fixable
        ]
        fixable = report.fixable_findings

        if policy == "off" or not fixable:
            await self.save_incident(report)
            return report
        if owner is None:
            report.simulation_verdict = "skipped: workflow owner no longer exists"
            await self.save_incident(report)
            return report

        try:
            patch_result = await self.patch(db, workflow_id, report)
        except WorkflowInterpretationError as exc:
            report.simulation_verdict = f"patch generation failed: {exc}"
            await self.save_incident(report)
            return report
        if patch_result.patched is None:
            report.simulation_verdict = "no patch produced"
            await self.save_incident(report)
            return report

        report.patches_applied = patch_result.changes
        report.validation_passed = True
        if patch_result.scope_warnings:
            # Never auto-apply a patch that edits nodes outside the finding's
            # blast radius (audit finding G) — leave it for a human.
            report.simulation_verdict = (
                f"auto-apply refused: out-of-scope edits {patch_result.scope_warnings}"
            )
            await self.save_incident(report)
            return report

        if policy == "safe":
            report.simulation_verdict = await self._apply_safe(
                db, owner, workflow, patch_result.patched, base_version
            )
            report.new_version_created = report.simulation_verdict.startswith("drafted")
        else:  # autonomous
            applied, published, note = await self._apply_autonomous(
                db, owner, workflow, patch_result.patched, base_version
            )
            report.new_version_created = applied
            report.published = published
            report.simulation_verdict = note

        await self.save_incident(report)
        return report

    async def _apply_safe(
        self,
        db: AsyncSession,
        owner: User,
        workflow: Workflow,
        patched: WorkflowDefinition,
        base_version: int,
    ) -> str:
        # Do not knock a live workflow into preview mode: only auto-draft a
        # workflow that is already a draft. For a published workflow, record the
        # proposed fix for a human to apply (audit finding A).
        if workflow.status == WorkflowStatus.PUBLISHED:
            return "published workflow left live; proposed fix recorded for human review"
        if not await self._acquire_lock(workflow.id):
            return "skipped: another heal holds the lock"
        try:
            fresh = await db.get(Workflow, workflow.id)
            if fresh is None or fresh.current_version != base_version:
                return "skipped: workflow changed during heal"
            await self._wf().update_workflow(
                db,
                user=owner,
                workflow_id=workflow.id,
                body=WorkflowUpdateBody(
                    definition=patched, change_note="self-heal (safe): drafted fix"
                ),
            )
            return "drafted a fix; awaiting human publish"
        finally:
            await self._release_lock(workflow.id)

    async def _apply_autonomous(
        self,
        db: AsyncSession,
        owner: User,
        workflow: Workflow,
        patched: WorkflowDefinition,
        base_version: int,
    ) -> tuple[bool, bool, str]:
        """Apply → verify (persisted demo) → publish, holding a lock and rolling
        back to the prior published version on failure. Returns
        ``(applied, published, note)``."""
        wid = workflow.id
        if not await self._acquire_lock(wid):
            return False, False, "skipped: another heal holds the lock"
        try:
            # Staleness / TOCTOU guard (audit finding H).
            fresh = await db.get(Workflow, wid)
            if fresh is None or fresh.current_version != base_version:
                return False, False, "skipped: workflow changed during heal"

            prior_status = fresh.status
            prior_pub_def: dict[str, Any] | None = None
            if prior_status == WorkflowStatus.PUBLISHED and fresh.published_version_id:
                pv = (
                    await db.execute(
                        select(WorkflowVersion).where(
                            WorkflowVersion.id == fresh.published_version_id
                        )
                    )
                ).scalar_one_or_none()
                if pv is not None:
                    prior_pub_def = pv.definition

            wf = self._wf()
            # 1. Apply (creates a new version; also unpublishes — see rollback).
            await wf.update_workflow(
                db,
                user=owner,
                workflow_id=wid,
                body=WorkflowUpdateBody(
                    definition=patched, change_note="self-heal (autonomous)"
                ),
            )
            # 2. Verify with a persisted demo run (satisfies the publish gate for
            #    the new version) + judge the transcript.
            verdict, reason = await self._verify_persisted(db, owner, wid)
            if verdict == "fail":
                note = await self._restore_note(
                    db, owner, wid, prior_pub_def, prior_status, f"verification failed ({reason})"
                )
                return True, False, note
            # 3. Only re-publish a workflow that was ALREADY live. Never push a
            #    workflow a human deliberately kept in draft to production
            #    (symmetric with safe mode, which also won't flip publish state).
            if prior_status != WorkflowStatus.PUBLISHED:
                return (
                    True,
                    False,
                    f"applied as draft; workflow was not previously published, "
                    f"left for human publish (verify: {verdict})",
                )
            try:
                await wf.publish_workflow(db, user=owner, workflow_id=wid)
            except Exception as exc:  # noqa: BLE001 — publish gate rejection
                note = await self._restore_note(
                    db, owner, wid, prior_pub_def, prior_status, f"publish gate failed ({exc})"
                )
                return True, False, note
            return True, True, f"applied and published (verify: {verdict})"
        finally:
            await self._release_lock(wid)

    async def _verify_persisted(
        self, db: AsyncSession, owner: User, workflow_id: uuid.UUID
    ) -> tuple[str, str]:
        """Run a persisted demo execution of the latest version and judge it."""
        wf = self._wf()
        parts: list[str] = []
        try:
            async for ev in wf.execute_workflow(
                db,
                user=owner,
                workflow_id=workflow_id,
                request_input={},
                variables={},
                demo=True,
                use_real_llm=True,
                branch_overrides={},
            ):
                etype = ev.get("type")
                if etype == "node_complete":
                    out = _preview(ev.get("output_snapshot"))
                    if out:
                        parts.append(f"{ev.get('node_id')}: {out}")
                elif etype == "agent_complete":
                    text = _event_text(ev)
                    if text:
                        parts.append(f"{ev.get('agent_id')}: {text}")
                elif etype == "error":
                    return "fail", ev.get("message") or _event_text(ev) or "demo run errored"
        except Exception as exc:  # noqa: BLE001 — a broken def must not crash the pass
            log.warning("healing.verify_persisted_failed", error=str(exc))
            return "fail", f"demo run raised: {exc}"
        return await self._judge("\n".join(parts)[:_TRANSCRIPT_CHARS])

    async def _restore_note(
        self,
        db: AsyncSession,
        owner: User,
        workflow_id: uuid.UUID,
        prior_pub_def: dict[str, Any] | None,
        prior_status: WorkflowStatus,
        why: str,
    ) -> str:
        """Attempt a rollback and return a note describing the resulting state —
        crucially flagging when a formerly-live workflow is left OFFLINE."""
        if prior_status != WorkflowStatus.PUBLISHED:
            # Was a draft: the failed heal remains an unpublished (non-live)
            # draft, which is safe. Nothing to restore.
            return f"{why}; left as unpublished draft (was not live)"
        restored = await self._restore(db, owner, workflow_id, prior_pub_def, prior_status)
        if restored:
            return f"{why}; rolled back to prior published version"
        return (
            f"{why}; ROLLBACK FAILED — workflow is OFFLINE (unpublished); "
            "human intervention required"
        )

    async def _restore(
        self,
        db: AsyncSession,
        owner: User,
        workflow_id: uuid.UUID,
        prior_pub_def: dict[str, Any] | None,
        prior_status: WorkflowStatus,
    ) -> bool:
        """Restore the previously-published definition after a failed autonomous
        heal. Returns True if the prior live state was restored (or there was
        nothing to restore); False if the workflow is left offline."""
        if prior_pub_def is None or prior_status != WorkflowStatus.PUBLISHED:
            return True
        try:
            wf = self._wf()
            prior_wd = WorkflowDefinition.model_validate(prior_pub_def)
            await wf.update_workflow(
                db,
                user=owner,
                workflow_id=workflow_id,
                body=WorkflowUpdateBody(
                    definition=prior_wd,
                    change_note="self-heal rollback: restore prior published version",
                ),
            )
            # Re-satisfy the publish gate with a demo run, then re-publish.
            async for _ in wf.execute_workflow(
                db,
                user=owner,
                workflow_id=workflow_id,
                request_input={},
                variables={},
                demo=True,
                use_real_llm=False,
                branch_overrides={},
            ):
                pass
            await wf.publish_workflow(db, user=owner, workflow_id=workflow_id)
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort restore
            log.error("healing.rollback_failed", workflow_id=str(workflow_id), error=str(exc))
            return False

    # ---- headless coordination helpers ----
    def _wf(self) -> Any:
        if self._wf_cache is None:
            from services.workflow_service import WorkflowService

            self._wf_cache = WorkflowService(self._settings)
        return self._wf_cache

    async def _resolve_owner(self, db: AsyncSession, workflow: Workflow) -> User | None:
        # Must be a CURRENT member of the workflow's workspace — every headless
        # write goes through membership checks, so a non-member creator would
        # 403 and be misattributed as a verification failure (audit finding).
        stmt = (
            select(User)
            .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
            .where(
                User.id == workflow.created_by,
                WorkspaceMember.workspace_id == workflow.workspace_id,
            )
            .limit(1)
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    async def _acquire_lock(self, workflow_id: uuid.UUID) -> bool:
        try:
            redis = get_redis()
            return bool(
                await redis.set(
                    _LOCK_KEY.format(workflow_id=workflow_id),
                    "1",
                    nx=True,
                    ex=_LOCK_TTL_SECONDS,
                )
            )
        except Exception as exc:  # noqa: BLE001 — no lock service → don't apply
            log.warning("healing.lock_unavailable", error=str(exc))
            return False

    async def _release_lock(self, workflow_id: uuid.UUID) -> None:
        with contextlib.suppress(Exception):
            await get_redis().delete(_LOCK_KEY.format(workflow_id=workflow_id))

    async def in_cooldown(self, workflow_id: uuid.UUID) -> bool:
        try:
            return bool(await get_redis().exists(_COOLDOWN_KEY.format(workflow_id=workflow_id)))
        except Exception:  # noqa: BLE001
            return False

    async def set_cooldown(self, workflow_id: uuid.UUID, seconds: int) -> None:
        with contextlib.suppress(Exception):
            await get_redis().set(
                _COOLDOWN_KEY.format(workflow_id=workflow_id), "1", ex=max(seconds, 1)
            )

    # ------------------------------------------------------------------ #
    # Shared helpers
    # ------------------------------------------------------------------ #
    async def _load_workflow(
        self, db: AsyncSession, workflow_id: uuid.UUID
    ) -> tuple[Workflow, WorkflowDefinition]:
        workflow = await db.get(Workflow, workflow_id)
        if workflow is None or workflow.deleted_at is not None:
            raise HealingError(f"workflow {workflow_id} not found")
        latest = (
            await db.execute(
                select(WorkflowVersion)
                .where(WorkflowVersion.workflow_id == workflow_id)
                .order_by(WorkflowVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            raise HealingError(f"workflow {workflow_id} has no versions")
        definition = WorkflowDefinition.model_validate(latest.definition)
        return workflow, definition

    def _azure_client(self):  # -> AsyncAzureOpenAI
        from openai import AsyncAzureOpenAI

        endpoint = (self._settings.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
        key = (self._settings.AZURE_OPENAI_API_KEY or "").strip()
        if not endpoint or not key:
            raise HealingError(
                "Azure OpenAI endpoint or API key missing "
                "(set AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY)."
            )
        return AsyncAzureOpenAI(
            azure_endpoint=endpoint,
            api_key=key,
            api_version=self._settings.AZURE_OPENAI_API_VERSION,
        )

    async def _call_llm(self, *, messages: list[dict[str, Any]]) -> str:
        deployment = (
            self._settings.AZURE_OPENAI_DEPLOYMENT
            or self._settings.AZURE_OPENAI_DEFAULT_MODEL
        )
        client = self._azure_client()
        completion = await client.chat.completions.create(
            model=deployment,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=messages,
        )
        content = completion.choices[0].message.content or ""
        if not content.strip():
            raise HealingError("diagnosis LLM returned empty content")
        return content


# ---------------------------------------------------------------------- #
# Module-level pure helpers
# ---------------------------------------------------------------------- #
def _detect_trigger_type(definition: WorkflowDefinition) -> str:
    for node in definition.iter_nodes():
        if getattr(node, "kind", None) == "trigger":
            return getattr(node, "trigger_type", "manual")
    return "manual"


def _patch_instruction(fixable: list[Finding]) -> str:
    lines = [heal_prompts.PATCH_INSTRUCTION_HEADER]
    for f in fixable:
        target = ", ".join(f.node_ids) if f.node_ids else "(graph-wide)"
        lines.append(
            f"- [{f.category}] nodes={target}: {f.summary}\n"
            f"  Fix: {f.proposed_fix}"
        )
    return "\n".join(lines)


def _available_tools(definition: WorkflowDefinition) -> list[str]:
    """Tool slugs already referenced by the workflow.

    A heal should not introduce brand-new integrations that need connections, so
    we constrain augment to the tools already in play.
    """
    tools: set[str] = set()
    for node in definition.iter_nodes():
        for slug in getattr(node, "tools", []) or []:
            if slug:
                tools.add(str(slug))
    return sorted(tools)


def _allowed_edit_ids(definition: WorkflowDefinition, fixable: list[Finding]) -> set[str]:
    """Node ids a patch is permitted to modify/remove: the findings' nodes plus
    a 1-hop neighborhood so legal re-wiring of adjacent nodes doesn't trip the
    scope guard. Newly-added node ids are always allowed (handled in the guard).
    """
    nodes = list(definition.iter_nodes())
    by_id = {n.id: n for n in nodes}
    # A graph-wide fix (a finding with no node_ids) is inherently unscoped —
    # don't let the guard flag every legitimate edit. Allow the whole graph.
    if any(not f.node_ids for f in fixable):
        return set(by_id)

    seed = {nid for f in fixable for nid in f.node_ids if nid in by_id}
    # Structural fixes (converge branches, insert a merge, re-point a shared
    # tail — see the augment prompt's convergence rules) legitimately re-wire
    # up to 2 hops out; other fixes stay tight at 1 hop.
    hops = 2 if any(f.category == "graph_structure_issue" for f in fixable) else 1

    allowed: set[str] = set(seed)
    frontier: set[str] = set(seed)
    for _ in range(hops):
        nxt: set[str] = set()
        for node in nodes:
            refs = set(getattr(node, "depends_on", []) or []) | set(
                (getattr(node, "activate_on", None) or {}).keys()
            )
            if refs & frontier:  # node depends on the frontier (downstream)
                nxt.add(node.id)
            if node.id in frontier:  # frontier node's upstream targets
                nxt |= refs
        nxt &= set(by_id)
        frontier = nxt - allowed
        allowed |= nxt
        if not frontier:
            break
    return allowed


def _scope_violations(
    *, before: WorkflowDefinition, after: WorkflowDefinition, allowed: set[str]
) -> list[str]:
    """Existing nodes modified or removed outside the allowed set. Added nodes
    are always permitted (a fix may legitimately introduce a node)."""
    # A legacy agents-only workflow that the patch migrated to the ``nodes``
    # shape can't be meaningfully diffed node-by-node (iter_nodes synthesizes
    # AgentNode defaults for the promoted agents), so every node would look
    # "modified". Skip the guard for that case.
    if not before.nodes:
        return []
    before_nodes = {n.id: n for n in before.iter_nodes()}
    after_nodes = {n.id: n for n in after.iter_nodes()}
    violations: list[str] = []
    for nid in before_nodes.keys() - after_nodes.keys():
        if nid not in allowed:
            violations.append(f"removed {nid!r}")
    for nid in before_nodes.keys() & after_nodes.keys():
        if nid in allowed:
            continue
        if before_nodes[nid].model_dump() != after_nodes[nid].model_dump():
            violations.append(f"modified {nid!r}")
    return violations


def _enum_value(value: Any) -> str:
    return getattr(value, "value", None) or str(value)


def _step_evidence(step: WorkflowExecutionStep) -> StepEvidence:
    return StepEvidence(
        node_id=step.node_id,
        node_name=step.node_name,
        node_kind=step.node_kind,
        status=_enum_value(step.status),
        error_message=step.error_message,
        duration_ms=step.duration_ms,
        output_preview=_preview(step.output_snapshot),
    )


def _step_evidence_from_event(ev: dict[str, Any]) -> StepEvidence:
    # Built from a demo ``node_complete`` event: output lives under
    # ``output_snapshot`` (a dict), not ``data``/``content``; ``node_kind`` and
    # ``duration_ms`` are present. Keep the ``data``/text fallbacks for other
    # event shapes.
    return StepEvidence(
        node_id=str(ev.get("node_id") or ev.get("agent_id") or ""),
        node_name=ev.get("node_name") or ev.get("agent_name"),
        node_kind=str(ev.get("node_kind") or ""),
        status=str(ev.get("status") or "completed"),
        duration_ms=ev.get("duration_ms"),
        output_preview=_preview(ev.get("output_snapshot") or ev.get("data") or _event_text(ev)),
    )


def _event_text(ev: dict[str, Any]) -> str:
    content = ev.get("content")
    return content if isinstance(content, str) else ""


def _preview(value: Any) -> str:
    if value in (None, "", {}, []):
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except (TypeError, ValueError):
            text = str(value)
    return text[:_OUTPUT_PREVIEW_CHARS]


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Best-effort extraction of a JSON object from an LLM response."""
    text = raw.strip()
    if text.startswith("```"):
        # strip ```json … ``` fences
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text.strip("`")
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise HealingError("diagnosis LLM did not return a JSON object") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise HealingError(f"diagnosis JSON parse failed: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HealingError("diagnosis LLM returned non-object JSON")
    return parsed


def _parse_findings(raw_findings: Any) -> list[Finding]:
    if not isinstance(raw_findings, list):
        return []
    findings: list[Finding] = []
    for idx, item in enumerate(raw_findings, 1):
        if not isinstance(item, dict):
            continue
        if not str(item.get("finding_id") or "").strip():
            item["finding_id"] = f"f-{idx}"
        if item.get("category") not in heal_prompts.VALID_CATEGORIES:
            item["category"] = "other"
        if item.get("severity") not in heal_prompts.VALID_SEVERITIES:
            item["severity"] = "medium"
        # LLMs frequently emit node_ids as a bare string; coerce so the whole
        # finding isn't dropped over a shape mismatch.
        nid = item.get("node_ids")
        if isinstance(nid, str):
            item["node_ids"] = [nid]
        elif isinstance(nid, list):
            item["node_ids"] = [str(x) for x in nid]
        elif nid is None:
            item.pop("node_ids", None)  # let default_factory=list apply
        else:
            item["node_ids"] = []
        try:
            findings.append(Finding.model_validate(item))
        except Exception as exc:  # noqa: BLE001 — skip malformed findings, keep the rest
            log.warning("healing.finding_parse_skipped", index=idx, error=str(exc))
    return findings
