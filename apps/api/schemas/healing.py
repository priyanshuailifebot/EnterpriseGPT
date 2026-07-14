"""Pydantic models for the self-healing subsystem.

Ports the ``Finding`` / ``HealingReport`` contract described in
``docs/SELF_HEALING_PLAN.md`` (from Dograh's Doctor graph) onto EnterpriseGPT's
UUID identifiers and multi-path executor.

Phase 1 scope is the *diagnosis* half of the pipeline: the structured findings a
``diagnose()`` call produces from gathered run evidence, plus the typed evidence
container ``gather_evidence()`` returns. The patch/validate/verify/apply fields on
``HealingReport`` are declared here but only populated by later phases.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from schemas.workflow import WorkflowDefinition

FindingCategory = Literal[
    "prompt_issue",
    "edge_condition_issue",
    "extraction_issue",
    "graph_structure_issue",
    "tool_failure",
    "integration_failure",
    "configuration_issue",
    "other",
]

Severity = Literal["low", "medium", "high", "critical"]
HealthStatus = Literal["healthy", "degraded", "broken", "unknown"]
TriggeredBy = Literal["chat", "api", "monitor"]
EvidenceSource = Literal["real_runs", "chat", "manufactured", "none"]


class Finding(BaseModel):
    """One diagnosed problem with a workflow.

    ``auto_fixable`` is the load-bearing gate: only findings the diagnosis marks
    auto-fixable become eligible for the (later-phase) patch/validate/apply path.
    Everything else surfaces as a "needs a human" item and is never touched
    automatically. The diagnosis prompt keeps live-integration and tool failures
    ``auto_fixable=False`` — a demo verification run cannot exercise them (see
    audit finding B in ``docs/SELF_HEALING_PLAN.md`` §0.5).
    """

    # LLM-produced: tolerate unknown keys rather than 500 on schema drift.
    model_config = ConfigDict(extra="ignore")

    finding_id: str = Field(min_length=1, max_length=64)
    category: FindingCategory = "other"
    severity: Severity = "medium"
    node_ids: list[str] = Field(default_factory=list)
    summary: str = Field(default="", max_length=2000)
    evidence: str = Field(default="", max_length=4000)
    root_cause: str = Field(default="", max_length=4000)
    proposed_fix: str = Field(default="", max_length=4000)
    auto_fixable: bool = False


class StepEvidence(BaseModel):
    """Compact view of one persisted ``WorkflowExecutionStep``."""

    model_config = ConfigDict(extra="forbid")

    node_id: str = ""
    node_name: str | None = None
    node_kind: str = ""
    status: str = ""
    error_message: str | None = None
    duration_ms: int | None = None
    output_preview: str = ""


class RunEvidence(BaseModel):
    """Compact view of one ``WorkflowExecution`` and its steps.

    ``steps`` is empty for agent-only (Dynamiq) runs, which persist no per-node
    step rows — the execution-level ``status``/``error_message``/``duration_ms``
    is then the only signal (audit finding C).
    """

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    status: str
    demo: bool = False
    error_message: str | None = None
    duration_ms: int | None = None
    started_at: str | None = None
    steps: list[StepEvidence] = Field(default_factory=list)


class EvidenceDigest(BaseModel):
    """Everything ``gather_evidence()`` collected for one workflow.

    ``source`` records *how* the evidence was obtained so ``diagnose()`` (and a
    human reader) knows how much to trust it:

    * ``real_runs`` — non-demo ``WorkflowExecution`` rows exist.
    * ``chat`` — chat-trigger workflow; evidence came from ``chat_sessions`` /
      ``chat_messages`` (chat workflows emit no ``WorkflowExecution`` rows).
    * ``manufactured`` — no run history existed, so a demo run produced the
      evidence. A demo run always "completes", so this is a structural smoke
      test, not proof the flow is healthy (audit finding B).
    * ``none`` — nothing could be gathered.
    """

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    workflow_name: str = ""
    trigger_type: str = "manual"
    source: EvidenceSource = "none"
    steps_available: bool = False
    runs: list[RunEvidence] = Field(default_factory=list)
    chat_notes: str = ""
    notes: str = ""

    def as_text(self, *, max_chars: int = 12000) -> str:
        """Render the digest as the prose block fed to the diagnosis LLM."""
        lines = [
            f"Workflow: {self.workflow_name or '(unnamed)'} ({self.workflow_id})",
            f"Trigger type: {self.trigger_type}",
            f"Evidence source: {self.source}",
            f"Per-node step detail available: {self.steps_available}",
        ]
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        if self.chat_notes:
            lines.append(f"\nChat evidence:\n{self.chat_notes}")
        if not self.runs:
            lines.append("\nNo run evidence available.")
        for i, run in enumerate(self.runs, 1):
            lines.append(f"\n--- Run {i}: {run.execution_id} ---")
            lines.append(
                f"status={run.status} demo={run.demo} duration_ms={run.duration_ms}"
                + (f" started_at={run.started_at}" if run.started_at else "")
            )
            if run.error_message:
                lines.append(f"error: {run.error_message}")
            if not run.steps:
                lines.append("(no per-node steps recorded — agent-only/Dynamiq run)")
            for step in run.steps:
                seg = f"  [{step.status}] {step.node_kind}:{step.node_id}"
                if step.node_name:
                    seg += f" ({step.node_name})"
                if step.duration_ms is not None:
                    seg += f" {step.duration_ms}ms"
                lines.append(seg)
                if step.error_message:
                    lines.append(f"      error: {step.error_message}")
                if step.output_preview:
                    lines.append(f"      out: {step.output_preview}")
        text = "\n".join(lines)
        return text[:max_chars]


class HealingReport(BaseModel):
    """The outcome of a heal attempt on one workflow.

    Phase 1 populates ``health``/``summary``/``findings``/``evidence_source``.
    The patch/validate/verify/apply fields below are placeholders that later
    phases fill in.
    """

    model_config = ConfigDict(extra="ignore")

    incident_id: str
    workflow_id: str
    workspace_id: str | None = None
    health: HealthStatus = "unknown"
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    evidence_source: EvidenceSource = "none"
    triggered_by: TriggeredBy = "chat"

    # Populated by later phases (patch → validate → verify → apply).
    patches_applied: list[str] = Field(default_factory=list)
    patches_proposed: list[str] = Field(default_factory=list)
    validation_passed: bool | None = None
    simulation_verdict: str = ""
    new_version_created: bool = False
    published: bool = False

    @property
    def fixable_findings(self) -> list[Finding]:
        """Findings the diagnosis marked auto-fixable (the patch candidates)."""
        return [f for f in self.findings if f.auto_fixable]


class PatchResult(BaseModel):
    """Outcome of the patch → sanitize → scope-guard loop.

    ``scope_warnings`` are non-empty when the LLM edited nodes outside the
    finding's declared blast radius even after the bounded repair loop. For an
    interactive heal these are surfaced at the propose gate for the human to
    judge; the autonomous tier (Phase 3) must treat them as hard failures.
    """

    model_config = ConfigDict(extra="forbid")

    patched: WorkflowDefinition | None = None
    changes: list[str] = Field(default_factory=list)
    scope_warnings: list[str] = Field(default_factory=list)
    sanitizer_notes: list[str] = Field(default_factory=list)
    required_providers: list[str] = Field(default_factory=list)
    repairs: int = 0


class VerifyResult(BaseModel):
    """Outcome of a demo-mode verification run.

    A demo run always "completes" (audit finding B), so ``reached_end`` alone is
    not proof of health — ``verdict`` comes from an LLM judge over the transcript
    and is the signal the autonomous tier will gate on.
    """

    model_config = ConfigDict(extra="forbid")

    ran: bool = False
    reached_end: bool = False
    error: str | None = None
    node_path: list[str] = Field(default_factory=list)
    verdict: Literal["pass", "warn", "fail", "unknown"] = "unknown"
    reason: str = ""


class SelfHealConfig(BaseModel):
    """Per-workflow autonomous self-heal policy (stored on ``workflows.self_heal``)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    policy: Literal["off", "safe", "autonomous"] = "safe"
    cooldown_seconds: int = Field(default=21600, ge=300, le=604800)


class HealRequest(BaseModel):
    """Body for ``POST /workflows/{id}/heal``.

    ``mode`` is accepted for forward-compatibility with the plan/ask/agent modes;
    the Phase 2 endpoint always stops at the propose gate (never writes),
    regardless of mode. ``simulate`` opts into a (cost-bearing) demo verification
    before proposing.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["agent", "plan", "ask"] = "agent"
    complaint: str = Field(default="", max_length=4000)
    selected_finding_ids: list[str] | None = None
    simulate: bool = False
