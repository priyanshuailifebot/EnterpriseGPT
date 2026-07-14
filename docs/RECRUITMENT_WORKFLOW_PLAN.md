# Recruitment Workflow → Production — File-Level Build Plan

> Goal: turn the demo-grade `_HR_RECRUITMENT` template into a **hardened,
> tenant-runnable, batch** recruitment pipeline comparable to a production n8n
> flow.
>
> **Decisions locked (2026-07-06):** fan-out architecture (W1–W4); a **human
> review gate before rejection**; build the **in-platform schedule-trigger
> runtime**. This doc is the concrete, file-level build plan (no code written
> yet).
>
> Companion context: the gap analysis that produced this plan (single-candidate
> hardcoding, broken candidate↔slot correlation, engine limits on
> `for_each`+HITL, no error/timeout/retry/schedule/notify). See §1 recap.

---

## 0.6. Architecture revision (2026-07-06): event-boundary decomposition

> **Supersedes the "fan-out with in-execution waits" model in §1–§3 below.**
> Implementation follow-up surfaced a hard engine fact: `wait_for_webhook`
> (`extended_executor._park_for_webhook`) is an **in-process busy-poll**, not a
> durable suspend — it holds the coroutine + DB session for the whole wait. That
> only survives while an SSE client is connected; a **headless (worker/cron)
> execution cannot durably park for days.** Decision (locked): model every
> human/async wait as an **execution boundary** — no long in-execution waits.

**Revised workflow chain** (short executions, correlated by `candidate_id`):

| WF | Trigger | Does | Ends by |
|---|---|---|---|
| **W1 Sourcing** | schedule/manual | fetch candidates → dedupe → `for_each`: send invite email (link → W2 form, carries signed `candidate_id`) | emails sent |
| **W2 Interview-start** | webhook/form (candidate submits slot) | validate signed candidate ctx → store slot/lang → `start_interview` (Retell) → store `call_id↔candidate_id` | call placed (does NOT wait for the call) |
| **W3 Scoring** | webhook (Retell **call-ended** event) | get transcript → `score_interview` → store → email summary → notify recruiter with signed approve/reject links | recruiter notified |
| **W4 Decision** | webhook (recruiter approve/reject click) | approve → check HR availability → schedule HR round; reject → mark `not_advanced` | scheduled / recorded |
| **W-Chaser** | schedule | query non-responders/stalled → remind; lapse after M | — |
| **W-Ranking** | schedule | query results → stack-rank → store | — |

The **human-review-before-rejection** gate is the W3→W4 boundary: rejection only
happens on the recruiter's explicit W4 action. The **voice interview is made
async** (W2 starts the call and ends; Retell's call-ended webhook drives W3) so
no execution runs for the multi-minute call duration.

**Impact on the platform items in §2:**
- **P1 (runner)** — still needed; drain-to-completion is correct because every
  execution is now short. Used by the scheduler + any programmatic run.
- **P2 (scheduler)** — ✅ **done**: `croniter` dep + `tasks/schedule_dispatcher.py`
  (`dispatch_due_schedules`) + a per-minute cron in `WorkerSettings` that enqueues
  `run_workflow_execution` for published workflows whose schedule trigger just
  came due. No catch-up (tick-window gated), multi-replica-safe via a per-slot
  `SET NX` marker, gated by `WORKFLOW_SCHEDULER_ENABLED` (default off). Tested in
  `tests/test_schedule_dispatcher.py` (6 cases).
- **P4 (failure routing + notify)** — ✅ **done** for action nodes: per-node
  `on_error: fail|continue|route` (`schemas/workflow._BaseNode`); the executor
  intercepts action errors and either fails (default), skips-and-continues, or
  sets an `ok`/`failed` decision so an error branch can gate via `activate_on`
  (mirrors IfNode). Emits a non-fatal `node_error` event. Tested in
  `tests/test_workflow_error_routing.py` (4 cases). Agent/data_store/for_each
  errors still default to `fail` — follow-up if needed.
- **P5 (action retries)** — ✅ done (`_run_action` reuses `_with_timeout_and_retry`).
- **P1 (runner)** — ✅ done (`tasks/workflow_runner.py`, registered in the worker).
- **P3 (fan-out dispatch / `internal.start_workflow`)** — **DROPPED.** The next
  step is triggered by an external event (form submit, Retell webhook, approve
  click), not by a workflow programmatically starting another. W1 just sends
  emails inside its `for_each` (an `action`, which loops support).
- **P6 (wait-timeout branch)** — **DROPPED.** No in-execution waits.
- **P7 (execution-scoped slot link)** → **P7′ (signed trigger context)** — ✅ **done**:
  `sign_trigger_context`/`verify_trigger_context` (short JWT) in `core/security.py`;
  an `internal.sign_link` action (`agents/action_runner.py`) builds signed email
  links; the webhook trigger route accepts a `ctx` token and merges the verified
  context into the execution input; a GET `/{id}/link/{slug}` landing route fires
  one-click email actions (recruiter approve/reject). Tested in
  `tests/test_trigger_signing.py` (5 cases).
- **NEW P9 — Retell call-ended webhook wiring** — ✅ **done**: an
  `internal.register_voice_route` action maps `call_id → {workspace, target
  trigger slug, candidate ctx}` in Redis; `POST /api/v1/voice/retell/callback`
  (`routers/voice.py`, secret-gated by `RETELL_WEBHOOK_SECRET`) looks that up on
  call-ended and fires the scoring workflow (resolved by workspace + webhook
  slug) as its owner. Tested in `tests/test_voice_callback.py` (4 cases). Point
  the Retell agent webhook at that endpoint (voice-mcp stays poll-based for
  start/status/transcript/score).

**Impact on templates (§3):** T1–T4 become the W1/W2/W3/W4 chain above plus
W-Chaser and W-Ranking; no template uses `wait_for_webhook` for a long human
wait. `data_store`-in-`for_each` is still avoided (W1 loop only emails; per-
candidate records are written once outside the loop or created by W2 on entry).

**Templates — ✅ done & validated.** `services/recruitment_templates.py` defines
the six `WorkflowDefinition`s (`HR_SOURCING`, `HR_INTERVIEW`, `HR_SCORING`,
`HR_DECISION`, `HR_CHASER`, `HR_RANKING`), registered in
`services/workflow_templates.py` (the old single `_HR_RECRUITMENT` removed).
They compose every new primitive: `for_each` batch email (W1), signed slot links
(P7′), Retell start + `register_voice_route` (P9), scoring + recruiter
approve/reject **human gate** (W3→W4), `on_error: route` failure branches on the
risky ATS/Retell calls (P4), and `schedule` triggers on the chaser/ranking (P2).
Cross-workflow addressing is by webhook-trigger slug via the id-free
`GET/POST /workflows/slug/{trigger_slug}` route (signed `ctx` carries the
workspace). All six validate on construction; wiring pinned by
`tests/test_recruitment_templates.py` (7 cases).

**Newly added platform bit for id-free correlation:** an
`internal.sign_link` that bakes the workspace into the token + a slug-based
trigger route (`_run_by_signed_slug`) that resolves the sibling workflow by
`(workspace-in-token, trigger slug)` — so templates never hardcode ids.

**ATS connector — ✅ scaffolded.** `ats` is now a real catalog provider
(`agents/native_providers.py`) — a bearer-HTTP connector (base_url + token) with
`tool_slugs=("ats_search_candidates",)`, connectable via the Connect modal. In
draft/demo runs it returns a sample shortlist (`_ats_demo_stub`) so the sourcing
template runs end-to-end without a live ATS; live runs POST `{jd, role, limit}`
to the tenant's endpoint (contract: returns a list / `{data:[…]}` of
`{candidate_id, name, email, phone, …}`). The tenant just connects their ATS URL.

**Frontend polish — ✅ done (editor).** The **schedule cron editor** already
existed in the node inspector; added the **`on_error` select**
(`PropertyInspector` ActionFields) + `on_error` on the frontend `_BaseNode` type,
so authors set fail/continue/route in the UI. tsc + eslint clean.

**Candidate slot-form page — ✅ done.** Public web page `apps/web/src/app/hr/slot/page.tsx`
(no auth) reads the signed `ctx` from the invite link, collects slot + language,
and POSTs to `/workflows/slug/hr-slot?ctx=…`. `sign_link` gained a `base:"web"`
option (+ `WEB_PUBLIC_URL` config) so W1's invite points at this page; the page
then hits the ctx-verified slug route. tsc + eslint clean.

**`on_error` inside `for_each` — ✅ done.** Per-item action failures now honor
the node's `on_error`: `continue`/`route` **isolate** the item (skip it, keep the
batch running); `fail` still aborts. W1's `send_invite` is `continue`, so one
candidate's email failure no longer kills the sourcing run. (Per-item error
*branches* — true `route` inside a loop — degrade to `continue`, since per-item
decisions aren't modeled; noted in code.) Tested in
`tests/test_workflow_error_routing.py`.

**Remaining = live wiring only:** connect the ATS URL + Retell/Google/Pipedream
credentials and flip the `AGENT_SELF_HEAL_*` / `WORKFLOW_SCHEDULER_ENABLED` /
`RETELL_WEBHOOK_SECRET` flags on. Everything else is built, tested, and dormant.

---

## 0. Why fan-out (recap of the binding engine constraints)

Verified against `apps/api/agents/extended_executor.py`:

- `for_each` bodies execute **only `agent` + `action` nodes inline**;
  `wait_for_webhook`, `data_store`, and nested control-flow inside a loop are
  **silently skipped** (`node_skipped: nested_control_flow_unsupported`).
- A `wait_for_webhook` is keyed by `(execution_id, node_id)`; **one active wait
  per execution**; its timeout is **fatal** (emits `error`, no branch).
- Any node error **aborts the whole execution**; there is no error edge, and
  top-level `ActionNode` `max_retries`/`timeout_ms` are **ignored** (retry only
  applies to actions invoked as agent *satellites*, via
  `agents/tool_resolver.py:_with_timeout_and_retry`).
- `schedule_cron` is a **defined-but-unwired** field (only `demo_executor`
  stubs it; no scheduler runs it).

⇒ A monolithic loop with per-candidate HITL is impossible today. The production
shape is **one execution per candidate**, dispatched asynchronously, plus a
cron-driven sourcing/chaser/rollup — i.e. fan-out.

---

## 1. Target architecture (W1–W4)

```
W1  Sourcing            trigger: schedule (cron) OR manual/webhook
    fetch candidates (ATS) → normalize + dedupe →
    for_each candidate:  action "internal.start_workflow"  → enqueues ONE W2 run
                                                              per candidate (async)

W2  Per-candidate pipeline   trigger: webhook (started by W1's dispatch)
    invite email (carries THIS execution's signed slot link)
      → wait_for_webhook "wait_slot"        ← one wait per execution ✓
      → data_store: schedule
      → agent: Retell voice interview  → action: score_interview
      → data_store: results  → email: summary
      → if score>75  → HUMAN REVIEW GATE (wait_for_webhook: recruiter approve)
                          → approved → check HR availability → schedule HR round
                          → rejected → mark not_advanced
                     → else (<=75) → mark below_threshold (also human-reviewable)
      → on any node failure → route to "notify recruiter + mark errored" (needs P4)

W3  Chaser              trigger: schedule (cron, e.g. daily)
    data_store query: status=email_sent AND slot_submitted=false AND age>N days
    → for_each: reminder email; after M reminders → mark lapsed

W4  Ranking rollup      trigger: schedule (cron) OR dispatched after a batch
    data_store query: interview_results for role → agent stack-rank → data_store write
```

Correlation key throughout: **`candidate_id`** (stable, from the ATS). Each W2
execution *is* one candidate's pipeline, so `execution_id` ↔ `candidate_id`.

---

## 2. Platform work items (prerequisite — the "tenant-runnable" bar)

Each item lists the **files to add/change** and the approach.

### P1 — Async workflow-execution primitive (shared by dispatch + scheduler)
The webhook route (`routers/workflows.py:881 webhook_trigger_route`) runs
`execute_workflow` **synchronously** and returns aggregated output — unusable
for W2 (parks for days) and for cron. Add a background runner.

- **New `apps/api/tasks/workflow_runner.py`**
  - `async def run_workflow_execution(ctx, *, workflow_id: str, input_data: dict, triggered_by: str) -> dict`
  - Opens its own session (`core.database.get_session_factory`), resolves the
    owner (`User where id == workflow.created_by`, mirroring
    `webhook_trigger_route:913` and the self-heal `_resolve_owner`), then
    **drains** `WorkflowService.execute_workflow(..., demo=False)` to completion.
    Returns `{execution_id, status}`.
- **`apps/api/tasks/worker.py`** — register `run_workflow_execution` in
  `WorkerSettings.functions` (alongside `monitor_and_heal`).
- Reuses the ARQ worker + `api-worker` compose service already added in the
  self-healing work (Phase 3). Redis pool already available.

### P2 — Schedule-trigger runtime (cron)
- **Add dep** `croniter` to `apps/api/pyproject.toml` (+ `poetry lock`); no cron
  lib exists today.
- **New `apps/api/tasks/schedule_dispatcher.py`**
  - `async def dispatch_due_schedules(ctx) -> dict`: scan **published** workflows
    (`select(Workflow).where(status==PUBLISHED)`), load each latest published
    `WorkflowVersion.definition`, find a `TriggerNode` with
    `trigger_type=="schedule"` + `schedule_cron`, and if due (compare
    `croniter` next-fire vs a per-workflow last-run marker in Redis
    `egpt:sched:last:{workflow_id}`), enqueue `run_workflow_execution` (P1).
  - Guard with a Redis lock per workflow (reuse the `_acquire_lock` pattern from
    `services/healing_service.py`) to avoid multi-replica double-fire.
- **`apps/api/tasks/worker.py`** — add a `cron(dispatch_due_schedules, minute=set(range(0,60,1)))`
  (every minute; the dispatcher itself decides due-ness) to `WorkerSettings.cron_jobs`.
- **`core/config.py`** — `WORKFLOW_SCHEDULER_ENABLED: bool = False` master switch
  (mirror `AGENT_SELF_HEAL_MONITOR`).

### P3 — Fan-out dispatch node (W1 → per-candidate W2)
Fan-out must be async and reference the sibling workflow by a stable handle
(template ids don't exist until instantiated), so use an **internal action**
(works inside `for_each`, which supports `action` nodes):

- **`apps/api/agents/native_providers.py`** (or the internal-tool registry) —
  register provider `internal`, action `start_workflow` with params
  `{workflow_slug: str, input: dict}`.
- **`apps/api/agents/action_runner.py`** — implement `start_workflow`: resolve
  `workflow_slug` → workflow id **within the same workspace**, then enqueue
  `run_workflow_execution` (P1) via the ARQ redis pool. Returns
  `{queued: true, target_workflow_id, correlation_id}` immediately (non-blocking).
- Resolution by workspace-scoped slug avoids the "template refers to an id that
  doesn't exist yet" problem (see T-notes).

### P4 — Failure routing + notify-on-failure
Today any node error aborts the run (`extended_executor` `_run_action:1740`,
service `workflow_service.py:1519`). Add author-controllable failure handling.

- **`apps/api/schemas/workflow.py`** — add `on_error: Literal["fail","continue","route"] = "fail"`
  to `_BaseNode` (or at least `ActionNode`/`AgentNode`), plus an optional
  `error_route: str` (a node id / decision label to activate on failure).
- **`apps/api/agents/extended_executor.py`** — in the node dispatch + the
  `depends_on`/`activate_on` machinery (`_activated:759`, prune loop `:785`):
  on node error, instead of unconditionally returning, record a `decisions[node_id]="failed"`
  so downstream `activate_on={"<node>":"failed"}` can route (mirrors the existing
  if/condition decision model). `"fail"` keeps today's abort; `"continue"` marks
  skipped; `"route"` fires the error branch.
- **`services/workflow_service.py`** — a workflow-level `on_failure` hook that,
  when an execution ends `FAILED`, fires a configured notification action
  (Slack/email) — or document that authors wire an explicit error branch (P4 +
  an action node). Recommend the branch approach for tenant control.

### P5 — Honor top-level action retries
`ActionNode.max_retries/timeout_ms/retry_initial_delay_ms` are ignored outside
the agent-satellite path.

- **`apps/api/agents/extended_executor.py:_run_action`** — wrap the
  `invoke_action(...)` call with the existing
  `agents/tool_resolver.py:_with_timeout_and_retry` (already battle-tested for
  satellites). No new logic — reuse.

### P6 — `wait_for_webhook` timeout → branch (not fatal)
- **`apps/api/agents/extended_executor.py:_park_for_webhook`** — on timeout,
  instead of emitting a terminal `error`, set `decisions[node_id]="timed_out"`
  and continue, so downstream `activate_on={"wait_slot":"timed_out"}` routes to a
  reminder/lapse path. (Complements W3, which is the primary non-responder
  mechanism; this makes single-execution timeouts non-fatal.)

### P7 — Execution-scoped slot link (per-candidate HITL correlation)
The invite email (sent *before* the wait node parks) must contain a link that
resumes *this* candidate's wait. The current resume token is minted only at park
time — chicken-and-egg. Two options; **recommend (a)**:

- **(a) Signed execution form link.** Add
  `GET /workflows/{id}/slot/{slug}?e=<execution_id>&sig=<hmac>` (renders the slot
  form) + `POST` the same (resolves the **single active parked wait** for that
  execution and injects the payload). Files:
  `routers/workflows.py` (new routes near `resume_webhook_route:668`), signing
  helper in `core/security.py`. The email template references
  `{{ execution.slot_url }}` — a new template var exposed by the executor at
  execution start (`extended_executor` context seeding).
- (b) Reserve a resume token at execution start and expose `{{ execution.resume_url("wait_slot") }}`;
  the wait node reuses the reserved token when it parks. More invasive to the
  token machinery; (a) is cleaner.

### P-note — what we deliberately are NOT building
Because we chose fan-out, we do **not** need `wait_for_webhook`/`data_store`
inside `for_each` (B1/B2). W1's loop only enqueues (P3, an action). If a future
monolith is desired, that engine work would be required instead.

---

## 3. Template work items (`apps/api/services/workflow_templates.py`)

Replace the single `_HR_RECRUITMENT` (lines 249–545) with four templates + add
them to `_TEMPLATES` (line 548) and `public_catalog`. All are `WorkflowTemplate`
(dataclass at line 37: `slug,title,summary,category,prompt,definition,required_integrations`).

- **T1 `_HR_SOURCING`** (slug `hr-sourcing`): schedule/manual trigger; `fetch_candidates`
  action (ATS — see §5); an agent/action normalize+dedupe; `for_each candidates`
  body = one `internal.start_workflow` action targeting slug `hr-candidate-pipeline`,
  passing `{candidate_id, name, email, phone, jd, role_title, language?}`.
  Fixes **A1** (batch), **A4** (phone threaded).
- **T2 `_HR_CANDIDATE_PIPELINE`** (slug `hr-candidate-pipeline`): webhook trigger
  keyed on `candidate_id`; invite email using `{{ execution.slot_url }}` (P7,
  fixes **A3**); `wait_slot` `wait_for_webhook`; schedule/interview/score/summary
  as today but referencing the single candidate (no `.0`, fixes A1/A2); the
  score branch adds a **`review_gate` `wait_for_webhook`** (recruiter approve)
  before HR scheduling; `activate_on` routes approve/reject/timeout. Idempotency
  via `data_store` `sent_*` guards (**C6**).
- **T3 `_HR_CHASER`** (slug `hr-chaser`): schedule trigger; `data_store` query for
  non-responders; `for_each` reminder email; lapse after M reminders.
- **T4 `_HR_RANKING`** (slug `hr-ranking`): schedule/dispatched; `data_store` query
  → stack-rank agent → `data_store` write. (This is the one piece that was
  already close.)

Cross-workflow reference is by **workspace-scoped slug** (`internal.start_workflow`
resolves slug→id at run time), so the templates don't hardcode ids.

---

## 4. Schema / model / migration changes

- **`apps/api/schemas/workflow.py`**
  - `_BaseNode.on_error` + `error_route` (P4).
  - Confirm `TriggerNode.schedule_cron` validation is a real cron (add a
    `field_validator` using `croniter`).
  - (No new node kind — fan-out uses the existing `action` kind via the
    `internal` provider.)
- **`apps/api/models/`** — no new table required if schedule last-run + locks
  live in Redis. *Optional:* a `workflow_schedule_state` column/table if you want
  durable last-run auditing (mirrors the self-heal decision to keep it in Redis).
- **Alembic** — only if the optional durable schedule-state is chosen; otherwise
  none.

## 5. ATS / résumé-source connector (you're providing the API)

- Define a **`fetch_candidates` action contract** the template depends on,
  returning a list of `{candidate_id, name, email, phone, resume_url, meta}`.
- Register the provider in **`apps/api/agents/native_providers.py`** +
  **`provider_normalizer.py`** (today `darwinbox`/`http_bearer` exist as generic
  entries). Swap `darwinbox_resume_search` for your ATS's action, or keep a
  generic `http_bearer` GET with your endpoint + a workspace connection holding
  the base URL + token.

## 6. Frontend changes (`apps/web`)

- **Schedule-trigger config UI** — a cron editor for `TriggerNode(schedule)` in
  the node inspector (`src/components/workflow/PropertyInspector.tsx`).
- **`internal.start_workflow` node** rendering + a workflow-slug picker in the
  inspector.
- **`on_error` / error-route** affordance on nodes (small select).
- **Human-review gate** — reuse the existing HITL surface
  (`useExecutionStream`/pending-HITL) so recruiters approve/reject in-app; the
  `review_gate` `wait_for_webhook` resumes via the same resume route.
- Templates gallery already lists via `GET /workflows/templates`
  (`templates/page.tsx`) — the four new templates appear automatically.

## 7. Compliance / governance (product)

- **Consent** — add a consent checkbox to the slot form; store on the candidate
  record; gate the Retell call on it (region-aware).
- **Retention/erasure** — a scheduled purge workflow (or a platform job) for
  résumés/transcripts past a retention window; wire to DPDP/GDPR erasure.
- **Human oversight** — the P4 review gate before rejection (chosen) satisfies
  "no fully-automated adverse decision"; log the human decision to
  `interview_results`.
- **Auditability** — transcripts + scores + decisions already land in
  `data_store`; add a read-only audit view.

---

## 8. Phased sequence & verification

1. **Platform Phase A** — P1 (runner) + P5 (action retries) + P4 (failure
   routing). Unit-test the runner + retry + error-branch.
2. **Platform Phase B** — P2 (scheduler) + P3 (dispatch) + P6 (wait timeout
   branch) + P7 (slot link). Integration-test cron fire + fan-out + resume.
3. **Template Phase C** — T1–T4 + the ATS connector (§5) + frontend (§6).
4. **Compliance Phase D** — §7.
5. **Verification** — end-to-end with a small real batch (real ATS pull → N
   parallel W2 executions → real Retell calls → recruiter approvals →
   HR scheduling), plus failure-injection (0 candidates, email bounce, no-slot
   timeout, call fail, score-parse fail) and a fan-out load test.

## 9. Open decisions / risks

- **Reminders vs. timeout branch** — W3 chaser (cron) is the robust non-responder
  path; P6 (per-execution timeout branch) is complementary. Confirm both, or W3
  only.
- **Notify-on-failure** — platform `on_failure` hook (auto Slack/email) vs.
  author-wired error branch (P4). Recommend author-wired branch for tenant
  control; a platform default alert is a nice add.
- **Scheduler cadence & multi-replica** — every-minute tick + per-workflow Redis
  lock; confirm acceptable vs. a dedicated scheduler.
- **Retell cost/consent at batch scale** — N parallel real calls per sourcing
  run; add a per-run cap + consent gate.
- **Idempotency** — `data_store` `sent_*` guards handle re-runs; confirm the key
  scheme (`candidate_id`).
