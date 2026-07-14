# EnterpriseGPT Self-Healing — Implementation Plan

> Design doc for porting Dograh's self-healing model onto EnterpriseGPT.
> Reference for the source pattern: [`SELF_HEALING_LOGIC.md`](../SELF_HEALING_LOGIC.md) (repo root).
>
> **Decisions locked (2026-07-03):** target the **full autonomous** tier;
> use **ARQ (Redis-backed)** for the headless monitor; ship in the phases below.

---

## 0. TL;DR

EnterpriseGPT already owns ~70% of the primitives Dograh's Doctor graph needs —
structured run history, an NL patch generator (`augment`), engine validation,
demo-mode simulation, immutable versioning with a publish gate, and
interrupt/resume patterns. Self-healing here is **assembly, not invention**. The
one real infra gap is a background task queue; we close it with **ARQ**, which
also unblocks Phase 4 scheduled triggers.

The build is a `HealingService` that composes existing services into the
Dograh pipeline:

```
gather_evidence → diagnose → [gate] → patch → validate(+repair≤2) → verify → apply → report
     │                          │        │           │                  │        │
 WorkflowExecution/Step     new LLM   augment()   model_validate    demo-mode  new WorkflowVersion
   (query recent runs)      call w/   (interpreter) + _validate_graph  execute   + publish gate
                            Finding                + repair loop      (verify)
                            schema
```

Two trigger surfaces, exactly like Dograh:
- **Interactive** — a "Diagnose & Heal" button, human-gated, needs no new infra.
- **Headless** — an ARQ cron that scans runs, flags unhealthy workflows, and
  heals them under a per-workflow policy (`off` / `safe` / `autonomous`).

---

## 0.5. Source-audit corrections (2026-07-03)

> An adversarial audit of this plan against the actual source (63 findings, 20
> confirmed by a skeptic re-check with file:line evidence) overturned several
> load-bearing assumptions. **Read this before building — the sections below
> that predate the audit still contain the original, partly-wrong wording; the
> corrections here supersede them.** The overall strategy stands; these change
> mechanics and, critically, the *autonomous* safety story.

**A. `apply` via `update_workflow()` takes a LIVE workflow OFFLINE.**
`update_workflow` unconditionally sets `status=DRAFT` and nulls `published_at` /
`published_version_id` on every save (`workflow_service.py:634-640`) — identical
to `unpublish`. Live side-effects only fire when `status==PUBLISHED`
(`workflow_service.py:1395`). So naively routing heal-apply through
`update_workflow` reverts a running production workflow to preview mode.
**Fix:** the HealingService needs a heal-aware apply that (interactive) warns the
user a published workflow will be unpublished until they re-publish, and
(autonomous) does apply → verify → **re-publish** as one atomic sequence so the
live window is momentary. Publish state is a single flag on the `Workflow` row,
**not** per-version.

**B. Demo-mode "verification" cannot fail — it is a smoke test, not a gate.**
`demo_executor.run_demo` always emits `workflow_complete(success=True)` with all
steps `completed` (`demo_executor.py:251-256, 717-726`); it only surfaces failure
if it *raises*, which it doesn't for logic errors. **Fix:** demote §3.6 from a
pass/fail safety gate to a "does it execute without raising + does an LLM judge
over the collected step outputs approve it" check. For `autonomous`, the judge
verdict — not `workflow_complete` — is the gate. Document that demo cannot
exercise real integrations/webhooks/HITL at all, so those finding categories
must stay `auto_fixable=False` for autonomous.

**C. Evidence is thin for the DOMINANT (agent-only) workflow type.**
Agent-only workflows run the Dynamiq path (`use_extended=False`), which never
emits `agent_complete`, so **no `WorkflowExecutionStep` rows are written** for
real runs (`dynamiq_service.py` stream never yields it; steps recorded only in
the extended, HITL, and demo paths). **Fix:** `gather_evidence` must fall back to
`WorkflowExecution`-level signal (`status`, `error_message`, `duration_ms`) for
agent-only workflows and lean on a manufactured demo run; the "richer than
Dograh" claim only holds for mixed-kind (extended) workflows.

**D. `execute_workflow` runs only the PERSISTED latest version — no patch override.**
It cannot verify an in-memory patch. **Fix:** either (i) call
`demo_executor.run_demo(definition=patched_wd, …)` directly on the in-memory
patch, or (ii) for autonomous, apply first (new version) then verify. The
autonomous order must be **patch → validate(+repair) → apply → verify → publish**
(not verify-before-apply, which would 409 the publish gate).

**E. The publish gate is satisfiable by a MOCKED run.** `publish_workflow`
requires a COMPLETED `WorkflowExecution` for the latest version but does **not**
filter on `demo` (`workflow_service.py:722-733`) — unlike `list_executions`. So a
demo verify run green-lights publish. Treat this as a **risk to close**, not a
convenience: gate autonomous publish on the judge verdict, not merely on the
gate being technically satisfied.

**F. "Free/instant rollback" does not exist.** Versions are immutable and
append-only, but there is **no rollback/restore endpoint** and `publish` does
**not** archive the prior version — it just repoints `published_version_id`.
**Fix:** rollback is a capability that must be *built* (a restore endpoint), not
an existing mitigation. Remove "prior version archived and restorable" from §5.4
/ §6 / §8.

**G. `augment` regenerates the WHOLE graph; id preservation is prompt-only.**
Not code-enforced (`workflow_interpreter.py:612-615`). A prompt variant is
insufficient. **Fix (§3.4/§3.5/§8):** add a deterministic post-augment scope
guard — compute allowed-edit id set = ∪ `findings[].node_ids` (+ their
`depends_on`/`activate_on` neighbors for legal re-wiring), run
`diff_definitions(before, after)` (`workflow_interpreter.py:692-724`), and
reject any modified/removed node outside that set. Also: augment's own retry is
**1 pass**, not `MAX_PATCH_REPAIRS=2` — the ≤2 loop and the `workflow_requirements()`
call are NEW wrappers the HealingService adds (augment gives only Pydantic +
`_validate_graph` for free).

**H. No locking on publish (TOCTOU).** `update_workflow`/`publish_workflow` run
READ COMMITTED with no row lock or advisory lock, and publish always promotes the
newest version. An autonomous publish racing a concurrent human edit can publish
the wrong version. **Fix:** take a per-workflow advisory lock (or a Redis lock)
around the autonomous apply→publish sequence, and skip the heal if the workflow
was edited after evidence was gathered.

**I. Clarification is turn-based re-invocation, not `interrupt()`/resume.**
`ClarificationService` runs a checkpoint-backed multi-round Q&A loop
(`analyze_initial` → `submit_answers`, `MAX_ROUNDS=3`, thread_id per session);
each turn runs the graph to END. Functionally reusable for Ask-mode, but it is
not a `resume_node` interrupt. Reword §1 row and §4.2 accordingly.

**J. Tenancy & identity gaps (headless path).**
- The monitor has **no request user** — every write must run as a resolved
  `User`. Resolve `Workflow.created_by` and thread it into
  `execute_workflow`/`update_workflow`/`publish_workflow` (mirror the webhook
  precedent at `routers/workflows.py:790-797`). New subsection **§5.6 Run-as identity**.
- `POST /{id}/heal` needs an explicit authz gate: `require_permission(
  Permission.WORKFLOW_CREATE)` (heal produces a draft = create-class; augment is
  already CREATE-gated).
- Add a **tenant-level kill switch** the monitor checks before scanning a
  workspace — e.g. a `self_heal` flag in the existing `Workspace.settings` JSONB
  — independent of per-workflow toggles.

---

## 1. Concept mapping: Dograh → EnterpriseGPT

| Dograh concept | EnterpriseGPT equivalent | Status | Notes |
|---|---|---|---|
| `build_doctor_graph()` state machine | `ExtendedWorkflowExecutor` + LangGraph HITL graphs | engine exists | We add a new `HealingService` pipeline rather than a graph clone |
| `investigate` / `get_run_evidence` | `WorkflowExecution` (always) + `WorkflowExecutionStep` (extended/HITL/demo paths only) | partial — see audit **C** | Per-node steps exist for mixed-kind workflows; **agent-only (Dynamiq) runs write no step rows** — fall back to execution-level status/error + a demo run |
| "simulate a call if no runs exist" | `execute_workflow(demo=True, use_real_llm=True)` | exists | Manufactures evidence for brand-new workflows |
| `diagnose` → typed `Finding`s | — | **missing** | New structured LLM call + Pydantic models |
| `auto_fixable` gate | — | **missing** | Add to `Finding` model |
| `patch` (LLM rewrites nodes/edges) | `WorkflowInterpreter.augment()` (current def + NL → proposed def, preserves ids) | exists | Biggest reuse win — already prompt-tuned |
| `validate_workflow_definition` | `WorkflowDefinition.model_validate` + `_validate_graph` (cycles, ref integrity, satellites) + `workflow_requirements()` | exists | Wrap in a repair loop; add a sanitizer step |
| `sanitize_workflow_definition` | — | **missing** | New deterministic pre-cleanup (see §4.4) |
| `verify` simulation + judge | `execute_workflow(demo=True)` + optional LLM judge | exists | Also satisfies the existing publish gate (needs a completed test run) |
| `save_workflow_draft` / `publish_draft` | immutable `WorkflowVersion` + `status` draft/published + `publish_workflow()` | exists | "Draft" = a new version at `status=draft`; publish archives prior version |
| `interrupt("questions")` | `ClarificationService` + `clarification_graph` (LangGraph checkpoints) | exists | Reuse for Ask-mode ("which findings?") |
| `interrupt("approval")` | editor `augment` → diff-preview → confirm; HITL `approve` endpoint | exists | Reuse for the propose/approve gate |
| `HealingReport` incident audit | — | **missing** | Redis list + optional DB table |
| headless monitor cron | **no task queue** (Celery/ARQ absent; scheduled triggers are Phase 4) | **missing** | **Build with ARQ** |
| `AGENT_SELF_HEAL_*` env policy | — | **missing** | Trivial to add |
| `_auto_apply_policy()` tiers | — | **missing** | `off` / `safe` / `autonomous` |

---

## 2. New data models

### 2.1 `schemas/healing.py`

```python
class Finding(BaseModel):
    finding_id: str
    category: Literal["prompt_issue", "edge_condition_issue", "extraction_issue",
                      "graph_structure_issue", "tool_failure", "integration_failure",
                      "configuration_issue", "other"]
    severity: Literal["low", "medium", "high", "critical"]
    node_ids: list[str]
    summary: str
    evidence: str
    root_cause: str
    proposed_fix: str
    auto_fixable: bool          # single flag that gates patch/validate/apply

class HealingReport(BaseModel):
    incident_id: str
    workflow_id: str            # UUID (EGPT uses UUID PKs, not int)
    workspace_id: str
    runs_analyzed: list[str]
    health: Literal["healthy", "degraded", "broken", "unknown"]
    findings: list[Finding]
    patches_applied: list[str]
    patches_proposed: list[str]
    validation_passed: bool | None
    simulation_verdict: str
    new_version_created: bool
    published: bool
    triggered_by: Literal["chat", "api", "monitor"]
```

`auto_fixable` is the load-bearing field, identical to Dograh: `True` findings
go through patch/validate/apply; everything else becomes a "needs you" bullet in
the report and is never touched automatically.

### 2.2 Per-workflow self-heal config

Add a nullable JSONB column `self_heal` to the `workflows` table (Alembic
migration), OR a `self_heal` block inside `WorkflowDefinition`. **Recommendation:
column on `workflows`** — it's operational config, not part of the versioned
graph, so it shouldn't create a new version when toggled.

```python
# shape stored in workflows.self_heal
{
  "enabled": bool,                    # default False
  "policy": "off" | "safe" | "autonomous",   # default "safe"
  "cooldown_seconds": int,            # default 21600 (6h)
}
```

This is the answer to *"how does a workflow, once created, get self-healing"* —
the author flips a toggle on the workflow, choosing the autonomy level. Default
new workflows to `enabled=False`.

### 2.3 Incident audit

- **Redis** (fast, capped): `egpt:heal:incidents:{workflow_id}` — `LPUSH` +
  `LTRIM 0 49`, mirroring Dograh's `append_incident`.
- **Durable** (optional, recommended for autonomous): a `healing_incidents`
  table (id, workflow_id, workspace_id, report JSONB, triggered_by, created_at)
  so autonomous auto-publishes have a permanent audit trail beyond Redis TTL.

---

## 3. The HealingService pipeline

Location: `apps/api/services/healing_service.py` + `apps/api/agents/healing/`
(`prompts.py`, `sanitizer.py`). It is an async pipeline (not a graph clone);
where a human gate is needed it uses the existing interrupt/resume or
diff-preview patterns.

### 3.1 `gather_evidence`

Query the last N `WorkflowExecution` rows for the workflow + their
`WorkflowExecutionStep`s. This is **better input than Dograh gets** — the steps
already carry `input_snapshot`, `output_snapshot`, `error_message`, `status`,
`duration_ms`. Assemble a compact evidence digest (recent run outcomes, failed
steps with errors, per-node timing anomalies).

If there are **no non-demo runs**, run one `execute_workflow(demo=True,
use_real_llm=True)` and use its SSE step stream as evidence — Dograh's
"I just built this, check it" trick.

### 3.2 `diagnose`

One structured LLM call through the existing Azure path
(`WorkflowInterpreter._call_llm` style, `temperature=0`, JSON mode) that turns
`evidence digest + workflow definition JSON` into a `HealingReport` of typed
`Finding`s. New prompt in `agents/healing/prompts.py`.

### 3.3 gate (mode branch)

Single decision point (mirrors Dograh's `diagnose_router`):

- **Headless**: follow the workflow's `self_heal.policy`. `off` or no
  auto-fixable findings → straight to `report`. Otherwise → `patch`.
- **Interactive**: honor the requested mode (agent/plan/ask). Agent → build then
  gate at `propose`; Plan → gate before building; Ask → interview which findings
  first. Nothing is written until the user approves.

### 3.4 `patch` — **reuse `WorkflowInterpreter.augment()`**

Feed the auto-fixable findings (filtered to the user's selection in Ask mode) as
the augment instruction. `augment()` already returns a proposed
`WorkflowDefinition` that preserves node ids and unrelated fields, and it already
has a validation-feedback retry loop. This is the single biggest reuse.

### 3.5 `validate` + repair (≤2)

Wrap the existing validation stack in a bounded repair loop:

1. **Sanitize** (new — `agents/healing/sanitizer.py`, see §4.4).
2. `WorkflowDefinition.model_validate()` — Pydantic schema.
3. `_validate_graph()` — cycles, `depends_on`/`activate_on` referential
   integrity, satellite rules.
4. `workflow_requirements()` — integration/connection readiness.

On failure, feed the exact error strings back into `augment()` and re-validate,
bounded at `MAX_PATCH_REPAIRS = 2` (Dograh's constant).

### 3.6 `verify` — **reuse demo-mode execution**

Run `execute_workflow(demo=True, use_real_llm=True)` on the patched definition,
walk the SSE events, and require it reaches `workflow_complete` with no errored
steps. Optionally add an LLM judge over the collected step outputs (Dograh-style,
calibrated not to penalize healthy multi-step flows). This verification also
satisfies the **existing publish gate**, which already requires a completed test
run before promotion.

### 3.7 `apply` — **reuse the versioning path**

`update_workflow()` creates a new immutable `WorkflowVersion` at `status=draft`
(this is EnterpriseGPT's "draft"). Emit a restorable checkpoint reference over
SSE. Only the headless **autonomous** tier then calls `publish_workflow()`,
which archives the prior published version — free rollback via the immutable
version history. Interactive heals always stop at a draft.

### 3.8 `report`

Persist the `HealingReport` to Redis (capped list) + the durable
`healing_incidents` table, and emit it as an SSE event shaped like existing
execution events so the frontend doesn't branch.

---

## 4. Interactive path (Phase 1–2, no new infra)

### 4.1 Endpoint

`POST /api/v1/workflows/{id}/heal` → `StreamingResponse` (SSE), body:
`{ mode: "agent"|"plan"|"ask", complaint?: str }`. Runs
`gather_evidence → diagnose → patch → validate → verify`, streams findings and
the proposed diff, and **stops before writing**.

### 4.2 Gate / approval

Reuse the editor's existing `augment` → diff-preview → confirm flow. The user's
confirm calls the normal `update_workflow` (creates the draft version). For a
conversational Ask-mode ("which of these should I fix?"), reuse
`ClarificationService`'s LangGraph-checkpoint interrupt pattern.

### 4.3 Web

Add a **"Diagnose & Heal"** action on the workflow detail/editor page that opens
the SSE stream, renders findings (grouped by severity), and shows the proposed
patch in the existing diff-preview component. This works day one — no scheduler.

### 4.4 Sanitizer (new)

`agents/healing/sanitizer.py` — deterministic cleanup of well-known LLM output
quirks *before* validation, so the repair loop doesn't oscillate:

- Drop edges whose source/target is a terminal/trigger node in an illegal
  direction (trigger can't have incoming; terminal can't have outgoing).
- Prune orphaned executable nodes (zero incoming `depends_on`, not a trigger)
  iteratively — removing one can orphan the next.
- Coerce obvious type mismatches the schema would reject (e.g. `branches` as a
  comma-string → list).

---

## 5. Headless autonomous path (Phase 3, ARQ)

### 5.1 ARQ setup

- Add `arq` to `apps/api` deps (Poetry). Redis is already in the stack
  (`docker-compose.yaml`), so no new service.
- New `apps/api/tasks/` package: `worker.py` (ARQ `WorkerSettings` with the
  cron + queued tasks), `self_healing.py` (the monitor + `_run_doctor`
  equivalent).
- Add an `arq apps.api.tasks.worker.WorkerSettings` process to compose/infra
  (a `worker` service alongside `api`). This same worker later runs Phase 4
  scheduled triggers.

### 5.2 Detection heuristics (per window, default 60 min)

Scan `WorkflowExecution` rows (excluding `demo`) per self-heal-enabled workflow;
flag unhealthy if **any** of (Dograh's thresholds):

```python
if total >= 2 and incomplete / total >= 0.4:      # ≥40% failed/incomplete
if total >= 3 and avg_duration_ms < some_floor:    # abnormally short (instant failures)
if stuck_count:                                     # runs stuck in running/pending > 30 min
```

### 5.3 Cooldown + bounded parallelism

- Redis cooldown key `egpt:heal:cooldown:{workflow_id}` (default 6h, from the
  workflow's `self_heal.cooldown_seconds`).
- Heal at most `AGENT_SELF_HEAL_MAX_PER_PASS` (default 3) workflows per tick,
  in parallel but isolated (`asyncio.gather` with per-task try/except) so one
  failure doesn't block the others.

### 5.4 Three-tier policy

Per-workflow `self_heal.policy`, with an env kill-switch:

- **off** — diagnose only, never write.
- **safe** — apply auto-fixable findings as a new **draft** version; a human
  publishes.
- **autonomous** — apply **and** `publish_workflow()` automatically once the
  patch passes validation **and** demo-mode verification; prior version archived
  and restorable. Zero human intervention.

`autonomous` **must** gate on both validation and a passing verification
simulation before publishing — this is the safety contract for the auto-publish.

### 5.5 Env vars

| Env var | Default | Effect |
|---|---|---|
| `AGENT_SELF_HEAL_MONITOR` | `false` | master switch — enables the ARQ cron |
| `AGENT_SELF_HEAL_AUTO_APPLY` | `safe` | global ceiling on per-workflow policy (`off`/`safe`/`autonomous`) |
| `AGENT_SELF_HEAL_SIMULATE` | `false` | also verify via simulation in `safe`; always on in `autonomous` |
| `AGENT_SELF_HEAL_COOLDOWN_SECONDS` | `21600` | default per-workflow cooldown |
| `AGENT_SELF_HEAL_MAX_PER_PASS` | `3` | workflows healed per cron tick |
| `AGENT_SELF_HEAL_WINDOW_MINUTES` | `60` | run-scan lookback window |

The env `AUTO_APPLY` acts as a **ceiling** over the per-workflow policy: a
workflow set to `autonomous` still only drafts if the env is `safe`. Interactive
heals ignore these entirely — they always gate on explicit human accept.

---

## 6. Mode-difference matrix

| | Plan | Ask | Agent | Headless (`safe`) | Headless (`autonomous`) |
|---|---|---|---|---|---|
| Builds patch before asking? | No | No | Yes | Yes | Yes |
| Interviews the user? | No | Yes | No | No | No |
| Validates before asking? | After approval | After approval | Yes | Yes | Yes |
| Runs verification sim? | After approval (if enabled) | After approval (if enabled) | After approval (if enabled) | If `SIMULATE=true` | Always |
| Requires human accept to write? | Yes | Yes | Yes | No | No |
| Writes as | Draft version | Draft version | Draft version | Draft version | Published (prior archived) |

---

## 7. Phased delivery

> **Status: Phases 1–3 shipped (2026-07-04).** All three phases are implemented,
> adversarially reviewed, and verified (see §7.1). The feature is gated **off**
> by default (`AGENT_SELF_HEAL_MONITOR=false`, `AUTO_APPLY=safe` ceiling,
> per-workflow opt-in) — safe to merge dormant.

**Phase 1 — models & diagnosis (no new infra)** ✅ shipped
- `schemas/healing.py`: `Finding`, `HealingReport`, `EvidenceDigest` (+ `RunEvidence`/`StepEvidence`).
- `agents/healing/prompts.py`: diagnose prompt + output contract.
- `services/healing_service.py`: `gather_evidence()`, `diagnose()`.
- As-built: evidence handles the agent-only (Dynamiq) path that writes **no**
  step rows (execution-level fallback), the chat surface (`chat_sessions`/
  `chat_messages`), and manufactured demo runs — labeled a smoke test, not proof.

**Phase 2 — patch/validate/verify + interactive endpoint** ✅ shipped
- `patch()` via `WorkflowInterpreter.augment()` + a **deterministic scope guard**
  (`diff_definitions`, 2-hop closure for structural fixes) + `≤2` repair loop.
- `agents/healing/sanitizer.py`: deterministic pre-validation cleanup.
- `verify()` via demo-mode run + LLM **judge** (not the always-true
  `workflow_complete` sentinel — audit B).
- `POST /workflows/{id}/heal` (SSE) + `GET /{id}/incidents`, stopping at the
  propose gate; membership-gated + `WORKFLOW_CREATE`.
- Web "Diagnose & Heal" panel (`HealPanel.tsx` + `useHealStream.ts`) reusing the
  canvas diff-preview / accept / save flow.
- Redis incident persistence (capped list).

**Phase 3 — headless autonomous (ARQ)** ✅ shipped
- ARQ worker (`tasks/worker.py`) + `tasks/self_healing.py` monitor (heuristics,
  cooldown, bounded parallelism, per-session isolation).
- `workflows.self_heal` column + Alembic migration `n8i9j0k1l2m3` + editor
  toggle UI (`SelfHealMenu.tsx`, `PUT /{id}/self-heal`).
- `AGENT_SELF_HEAL_*` env tiers; **safe** never touches a live workflow,
  **autonomous** only *re-publishes* previously-live workflows and is gated on
  the judge verdict + clean scope, with a Redis **lock**, staleness guard,
  member-resolved **run-as** identity, tenant kill-switch, and automatic
  **rollback** (+ `POST /{id}/rollback`) that flags "OFFLINE — human required"
  if restore fails.
- `api-worker` service in `docker-compose.yml`; `arq` in `pyproject.toml`.
- **Deviation from the original plan:** the `healing_incidents` durable DB table
  was **not** built — incidents persist to a capped, non-expiring Redis list,
  which is durable enough for now. Revisit if audit needs to outlive Redis.

### 7.1 Verification (2026-07-04)

Run in a Python 3.12 env (project excludes 3.14) against local Postgres + Redis.

- **Frontend:** `pnpm install` clean; **`tsc --noEmit` exit 0**; **eslint clean**
  on all new files. (Fixed one real type error: the SSE cast needed
  `as unknown as HealEvent`.)
- **arq API:** verified against arq 0.26.3 — `RedisSettings.from_dsn` and
  `cron(minute=…, run_at_startup=…)` correct; `WorkerSettings` loads with the
  full app stack.
- **Backend:** `poetry lock` regenerated with `arq`; `poetry install` OK; all new
  modules import cleanly.
- **Migration:** full Alembic chain applies from scratch through the new head
  `n8i9j0k1l2m3`; `workflows.self_heal` confirmed present as `jsonb`.
- **Tests:** the suites touching changed code — `test_publish_gate`,
  `test_workflow_augment`, `test_tools_agent_schema`, `test_provider_normalizer`
  — **34 passed, 0 failed**. The single failure elsewhere
  (`test_workflows.py::test_hitl_stream…`) is **environmental**: the LangGraph
  Redis checkpointer needs RediSearch (Redis Stack), and the local `redis` image
  is vanilla — unrelated to this change (no HITL/LangGraph code was touched).

**Still pending before enabling in a real environment:**
- CI/prod must `poetry install` the updated lock and `alembic upgrade head`.
- Turn-on is a deliberate, staged decision: start `safe`; only set
  `autonomous` once the judge/rollback/lock behavior has been observed on real
  incidents. Enable RediSearch (Redis Stack) if the HITL checkpointer is used.

---

## 8. Risks & open items

- **Autonomous auto-publish is the highest-risk surface — and audit findings A/B/D/E/F/H weaken the safety story the original draft assumed.** Real mitigations
  available today: mandatory validation + the scope guard (audit G),
  per-workflow opt-in defaulting off, env ceiling, cooldown, tenant kill-switch
  (audit J). Mitigations that must be BUILT before autonomous is safe: an LLM
  **judge**-based verify (audit B — demo `workflow_complete` is not a real gate),
  a **rollback/restore endpoint** (audit F — immutable history alone gives no
  instant rollback), a **lock** around apply→publish (audit H), and a
  notification (email/Slack) on every autonomous publish. **Recommendation:
  ship `safe` (draft-only) first; do not enable `autonomous` until the judge,
  rollback endpoint, and lock exist.**
- **Verification fidelity.** Demo-mode execution doesn't hit real integrations,
  so it can't catch live connector failures — it validates graph/flow soundness,
  not third-party behavior. Document this limit; keep `integration_failure`
  findings `auto_fixable=False` unless the fix is purely config.
- **Multi-replica scheduling.** ARQ handles distributed queuing, but ensure only
  one cron fires per window (ARQ cron jobs are singleton per schedule — fine).
- **Cost.** Diagnose + patch + verify are 3+ LLM calls per heal; the monitor
  multiplies that across flagged workflows. `MAX_PER_PASS` + cooldown bound it.
- **`augment()` scope creep.** It's tuned for user-driven edits; confirm it
  respects "only touch the flagged nodes" when driven by findings. May need a
  healing-specific system-prompt variant.

---

## 9. Key files (as built)

| File | Change | Status |
|---|---|---|
| `apps/api/schemas/healing.py` | `Finding`, `HealingReport`, `EvidenceDigest`, `PatchResult`, `VerifyResult`, `HealRequest`, `SelfHealConfig` | ✅ |
| `apps/api/services/healing_service.py` | the full pipeline (evidence/diagnose/patch/verify/heal + headless apply/publish/rollback) | ✅ |
| `apps/api/agents/healing/prompts.py` | diagnose + patch-instruction + judge prompts | ✅ |
| `apps/api/agents/healing/sanitizer.py` | deterministic pre-validation cleanup | ✅ |
| `apps/api/services/workflow_interpreter.py` | reused `augment()` / `diff_definitions()` (unchanged) | ✅ reuse |
| `apps/api/services/workflow_service.py` | reused `execute_workflow`/`update_workflow`/`publish_workflow`; added `rollback_workflow`, `set_self_heal` | ✅ |
| `apps/api/routers/workflows.py` | `POST /{id}/heal` (SSE), `GET /{id}/incidents`, `POST /{id}/rollback`, `PUT /{id}/self-heal` | ✅ |
| `apps/api/tasks/worker.py` | ARQ `WorkerSettings` + cron | ✅ |
| `apps/api/tasks/self_healing.py` | monitor (`monitor_and_heal`) | ✅ |
| `apps/api/models/workflow.py` | `self_heal` JSONB column | ✅ |
| `apps/api/models/healing_incident.py` | durable audit table | ❌ not built — Redis capped list used instead (see §7) |
| `apps/api/migrations/…n8i9j0k1l2m3…` | Alembic: `self_heal` column | ✅ |
| `apps/web/src/components/workflow/{HealPanel,useHealStream,SelfHealMenu}.tsx` | Diagnose & Heal panel + self-heal toggle | ✅ |
| `apps/web/src/{stores/workflowStore.ts,types/api.ts}` | `setSelfHeal` action + heal/self-heal types | ✅ |
| `docker-compose.yml` | `api-worker` (ARQ) service | ✅ |
| `apps/api/pyproject.toml` / `.env.example` | `arq` dep + `AGENT_SELF_HEAL_*` vars | ✅ |
