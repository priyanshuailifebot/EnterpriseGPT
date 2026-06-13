# Integration Plan — Curl Agents & Chat‑Prompt Workflow Editing

**Project:** EnterpriseGPT (NL → agentic workflows on Dynamiq)
**Date:** 2026-06-13
**Scope:** Two features —
1. **Curl agents** — let workflows make arbitrary HTTP/curl requests (any URL, method, headers, body) as a first‑class capability.
2. **Chat‑prompt workflow editing** — from a saved workflow in the library, open a chat panel, type instructions, and have the graph edited live (modify existing nodes **and** add new nodes), iteratively.

> **Headline finding:** *Both features are ~60–70% already built.* This is an **extend‑and‑finish** job, not a greenfield one. The plan below is deliberately framed around the existing primitives so we add the minimum new surface area.

---

## 0. Architecture recap (verified against the code)

| Layer | Path | Role |
|---|---|---|
| Engine (Dynamiq) | `dynamiq/dynamiq/nodes/` | Node base class, agents, tools. Node `type` = `"{module}.{ClassName}"`, resolved by `NodeManager.get_node_by_type` (`dynamiq/dynamiq/nodes/managers.py`). |
| HTTP tool | `dynamiq/dynamiq/nodes/tools/http_api_call.py` | `HttpApiCall(ConnectionNode)` — full curl: GET/POST/PUT/DELETE/PATCH, headers, params, body, files, timeout, success codes. **Already exists.** |
| Provider catalog | `apps/api/agents/native_providers.py` | `NativeProvider` entries map a provider → tool **slugs** → live Dynamiq tool via `build_connection`/`build_tool`. Includes `http_bearer` (`http_post`,`http_get`). |
| Tool factory | `apps/api/agents/native_tool_factory.py` | `build_native_agent_tools()` resolves an agent's declared slugs → instantiated tool nodes at run time; `available_native_tool_slugs()` is the flat catalog. |
| Workflow schema | `apps/api/schemas/workflow.py` | Polymorphic `NodeDefinition` union discriminated on `kind` (`agent`, `action`, `condition`, `if`, `for_each`, `merge`, `wait_for_webhook`, `trigger`, `data_store`, `memory`, `output_parser`, `human_handoff`). `AgentNode.tools: list[str]` = slugs. `ActionNode` = `provider`+`action_slug`+`params`. |
| NL interpreter | `apps/api/services/workflow_interpreter.py` | `interpret()` (NL→new) and **`augment()` (NL→edit existing)**, both Azure OpenAI, JSON‑mode, 2‑attempt validate/retry. `diff_definitions()` produces a human‑readable change list. |
| Routes | `apps/api/routers/workflows.py` | `POST /workflows/interpret`, **`POST /workflows/{id}/augment`** (returns `proposed_definition`+`changes`, does NOT persist), `POST /workflows/`, `PUT /workflows/{id}`, publish/unpublish. |
| Persistence | `apps/api/models/workflow.py`, `workflow_version.py`, `services/workflow_service.py` | `Workflow` + immutable `WorkflowVersion` rows (JSONB `definition`). `update_workflow` bumps version + resets to draft. |
| Frontend | `apps/web` (Next.js App Router, `@xyflow/react`, Zustand) | Builder `components/workflow/WorkflowBuilder.tsx`; canvas `InteractiveCanvas.tsx`; palette `NodePalette.tsx` (reads `NODE_KIND_CATALOG` in `workflow-mutations.ts`); inspector `PropertyInspector.tsx`; **`AIRefineDrawer.tsx`** (single‑shot augment); chat `components/chat/ChatPanel.tsx` + `hooks/useChatSession.ts`. |

---

# Feature 1 — Curl Agents

## 1.1 Goal
Allow a workflow (an agent, or a deterministic step) to issue **arbitrary HTTP requests** where the **URL, method, headers, and body are chosen at call time** (by the LLM agent) or configured per‑node — i.e. a real `curl`, not a fixed endpoint.

## 1.2 What already exists (and why we don't start from zero)
- `HttpApiCall` (`dynamiq/dynamiq/nodes/tools/http_api_call.py:142`) is a complete curl engine. Its `HttpApiCallInputSchema` already accepts `url`, `method`, `headers`, `params`, `data`, `files` **at execution time** — so an agent can drive a fully dynamic request if the tool is exposed that way.
- A `http_bearer` provider (`native_providers.py:915`) already exposes `http_post`/`http_get` slugs.

## 1.3 The gap
The current `http_bearer` binding is **not** a general curl:
- `_http_bearer_conn` (`native_providers.py:296`) hard‑pins `method=POST` and a single `base_url`, and forces a Bearer token + JSON content type.
- `_http_bearer_tool` (`native_providers.py:312`) builds an `HttpApiCall` with **no `url`/`method` defaults**, telling the agent to smuggle the path through an `additional_path` body field. That's awkward and bound to one host.

So there is no clean "call any URL with any method/headers" capability, and nothing surfaced in the node palette / interpreter as "curl".

## 1.4 Design decision — three levels, recommend Level 2

| Level | What | Effort | When |
|---|---|---|---|
| **L1** | Reuse `http_bearer` as‑is. | none | Single known host w/ bearer token. Already works; nothing to do. |
| **L2 (recommended)** | Add a dedicated **`curl` native provider** with a `curl_request` slug → an `HttpApiCall` whose URL/method/headers/body are supplied per call. Surface it in the tool catalog + interpreter + UI. | ~½–1 day | Generic "hit any API" capability for agents and for `action` nodes. |
| **L3** | Add a brand‑new **`curl` node `kind`** to the polymorphic schema + interpreter + frontend renderer/inspector. | ~2–3 days | Only if we want curl to be a visually distinct first‑class node rather than an `action`/agent‑tool. |

**Recommendation: Level 2.** The schema already has an `ActionNode` (`provider`+`action_slug`+`params`) and `AgentNode.tools: list[str]`. A `curl` provider plugs into *both* with zero schema/migration changes: an agent lists `"curl_request"` in `tools`, or a deterministic step is an `action` node with `provider="curl"`, `action_slug="curl_request"`, `params={url, method, headers, body}`. We avoid a new discriminator, a new Pydantic model, frontend union changes, and a DB consideration.

> Skip L3 unless product specifically wants a distinct "Curl" box on the canvas. If so, §1.7 lists the extra files.

## 1.5 Implementation — Level 2 (step by step)

### Backend / engine
1. **Add a curl connection + tool builder** in `apps/api/agents/native_providers.py`:
   - `_curl_conn(cfg)` → `Http(url=cfg.get("base_url",""), method=HTTPMethod.GET, headers={...optional defaults...})`. `base_url` optional (agent passes full `url` at call time); optional default headers (e.g. an API key) from `cfg`.
   - `_curl_tool(conn, slug)` → `HttpApiCall(name="curl_request", connection=conn, description=<<curl usage doc>>)`. The description must tell the agent it controls `url`, `method`, `headers`, `params`, `data`, and that `response_type` defaults to JSON-or-raw. Do **not** pin `method`/`url` on the node so the `input_schema` values win at call time.
2. **Register the provider** — append a `NativeProvider(id="curl", name="HTTP / curl", category="custom", auth_type="api_key", fields=(base_url optional, default_headers optional, api_key optional), tool_slugs=("curl_request",), build_connection=_curl_conn, build_tool=_curl_tool, probe=_probe_http_bearer-style)`. This automatically flows into `available_native_tool_slugs()` (`native_tool_factory.py:158`) and the `GET /integrations/tools` catalog the frontend already reads.
3. **Verify `build_native_agent_tools`** (`native_tool_factory.py:107`) resolves `"curl_request"` → provider `curl`. Because it keys off `tool_slugs`, registration alone is sufficient — confirm with a unit test.
4. **(If no connection required)** decide whether `curl` may run **without** a saved workspace connection (public APIs). `HttpApiCall` is a `ConnectionNode` and requires a `connection` (`node.py:1956` validator). Provide a default no‑auth `Http()` connection in `build_native_agent_tools` when a `curl` slug is requested but no connection row exists, mirroring the action‑node dry‑run path.

### Interpreter (so NL can produce curl steps)
5. In `apps/api/services/workflow_interpreter.py`, add `curl_request` to the documented tool vocabulary in `INTERPRETER_SYSTEM_PROMPT` **and** `AUGMENT_SYSTEM_PROMPT`, with a one‑line rule: *"For 'call this API / hit this URL / POST to X', prefer an `action` node with provider `curl`, action_slug `curl_request`, params `{url, method, headers, body}`; or add `curl_request` to an agent's `tools` when the agent must decide the request dynamically."* The interpreter receives `available_tools` at call time, so the slug also needs to be in that list (it will be, via the catalog).

### Frontend
6. **Palette / catalog** — `curl` is a *tool slug*, not a node kind, so no `NODE_KIND_CATALOG` change is needed. It appears automatically in the agent tool picker (sourced from `GET /integrations/tools`) and as a selectable `provider`/`action_slug` in `ActionFields` of `PropertyInspector.tsx:376`.
7. **Action inspector ergonomics** — when `provider==="curl"`, render friendly fields for `url`, `method` (dropdown), `headers` (JSON), `body` (JSON) that write into `params`, instead of raw JSON only. Optional polish; the generic `JsonField` already works.
8. **Connect modal** — the new provider's `fields` auto‑render in the existing Connect/credentials modal (driven by `ProviderField`), so users can store a default base URL / API key.

### Tests
9. `apps/api/tests/` — unit test that `available_native_tool_slugs()` includes `curl_request`; that `build_native_agent_tools` on a definition with an agent declaring `curl_request` returns an `HttpApiCall`; an executor test issuing a GET to a mock server with a runtime‑supplied URL/method.

## 1.6 Edge cases & guardrails
- **SSRF / egress:** arbitrary‑URL curl is a security surface. Add an allowlist/denylist (block RFC‑1918, `localhost`, cloud metadata `169.254.169.254`) in `_curl_tool` execution or a wrapper. Gate behind a workspace setting if needed.
- **Secrets:** default headers may carry API keys — store via the connection (already secret‑typed `ProviderField`), never inline in `params`.
- **Timeouts/retries:** `HttpApiCall.timeout` (default 30s) and `ActionNode.max_retries`/`timeout_ms` already exist — wire the action‑node values through.
- **Response size:** large bodies into the LLM context — consider truncation in the tool description / a `max_response_bytes`.

## 1.7 If product wants L3 (a distinct Curl node kind) — extra files
- `apps/api/schemas/workflow.py`: new `CurlNode(_BaseNode)` (`kind="curl"`, `url`,`method`,`headers`,`body`,`response_type`), add to `NodeDefinition` union + validators.
- Interpreter prompts: document the new kind.
- Executor: map `kind=="curl"` → `HttpApiCall`.
- Frontend: `NODE_KIND_CATALOG` + `makeBlankNode` (`workflow-mutations.ts`), a `CurlFlowNode` renderer (`visual-editor-renderers.tsx`), `CurlFields` (`PropertyInspector.tsx`), `WorkflowNode` union (`types/api.ts`), palette icon (`NodePalette.tsx`).

---

# Feature 2 — Chat‑Prompt Editing of Saved Workflows

## 2.1 Goal
From a workflow saved in the library (`/workflows/{id}`), open a **chat** surface, type instructions ("add a Slack notification after scoring", "make the condition route on refund amount", "add a new agent that summarizes the ticket"), and have the **graph edited live** — modifying existing nodes and **adding new nodes** — iteratively across turns, with preview‑then‑save.

## 2.2 What already exists
- **Backend augment is done.** `POST /workflows/{id}/augment` (`routers/workflows.py:293`) → `WorkflowInterpreter.augment()` (`workflow_interpreter.py:515`) using `AUGMENT_SYSTEM_PROMPT` (`:320`), with the same validate/retry as `interpret`. It returns `AugmentResponse { proposed_definition, changes[] }` and **does not persist** — the client previews then `PUT`s to save. The prompt already covers add/remove/connect/converge and **preserving stable node ids** (critical for clean canvas diffs).
- **`diff_definitions()`** (`workflow_interpreter.py:592`) classifies added/removed/modified nodes for the change list.
- **Frontend single‑shot UI exists:** `AIRefineDrawer.tsx` posts one instruction, applies the returned definition to the editor store, closes. Wired in `WorkflowBuilder.tsx:284` (`refineWithAI`) and enabled only when `savedWorkflowId` exists (`:418`).
- **Chat UI patterns exist:** `components/chat/ChatPanel.tsx` + `hooks/useChatSession.ts` (SSE, message history, tool‑call rendering) — the visual language to reuse.

## 2.3 The gap
The augment flow is **single‑shot and stateless**, not a **conversation**:
1. `AIRefineDrawer` is one textarea → apply → close. No transcript, no follow‑ups, no per‑turn diff history, no undo of a bad turn.
2. `augment()` takes only `(current_definition, user_message)` — it has **no conversation memory**, so turn N can't reference "the agent you just added." Each turn must re‑send the *latest* definition (which the client already holds), but multi‑turn intent ("now also…", "undo that", "rename it") needs history.
3. No clear **preview/accept/reject per turn** before mutating the canvas, and no path to save the accumulated result as a new version.

## 2.4 Design decision

> **Reuse the existing `augment` endpoint; make it conversational on top.** Do **not** build a new generation engine.

Two backend options:

| Option | Approach | Effort | Recommendation |
|---|---|---|---|
| **A (recommended)** | Keep `/augment` stateless; the **client** holds the running `definition` + a transcript, and sends `{message, current_definition, history[]}` each turn. Add an optional `history: list[{role, content}]` param to `AugmentRequest` and thread it into the LLM messages in `augment()`. | ~1–1.5 days | Minimal backend change, no new tables, naturally resumable, definition stays the source of truth. |
| **B** | Server‑side **edit session** (new table `workflow_edit_sessions`, like clarification's LangGraph checkpoint flow) storing transcript + working definition, with `POST /workflows/{id}/edit-sessions` + `/messages`. | ~3–4 days | Only if we need server‑persisted multi‑device edit transcripts or audit of every turn. Overkill for v1. |

**Recommendation: Option A.** It turns the existing single‑shot augment into a chat with one additive, backward‑compatible request field, and the canvas editor store already is the working‑copy source of truth.

## 2.5 Implementation — Option A (step by step)

### Backend
1. **Extend `AugmentRequest`** (`schemas/workflow.py`, ~`:886`) with `history: list[ChatTurn] = []` (each `{role: "user"|"assistant", content: str}`, capped length, e.g. last 10 turns). Keep `message` + `current_definition` as today (backward compatible).
2. **Thread history into `augment()`** (`workflow_interpreter.py:515`): build the LLM `messages` as `[system=AUGMENT_SYSTEM_PROMPT, ...history (as prior user instructions + short assistant "changes" summaries), user=current_definition+message]`. Keep the 2‑attempt validate/retry intact. Cap total tokens.
3. **Strengthen `AUGMENT_SYSTEM_PROMPT`** with explicit add‑node guidance for conversational follow‑ups: *"The conversation may reference nodes added in earlier turns by name; resolve them against the CURRENT definition. When adding a new node, return the full node object with a fresh snake_case id and correct `depends_on`."* (Add/remove rules already exist — this just makes follow‑up references reliable.)
4. **Return per‑turn diff** — `AugmentResponse` already carries `changes[]`; surface it per turn for the transcript. No change needed beyond passing it back to the chat UI.
5. **Persistence unchanged** — "Save" still calls `PUT /workflows/{id}` (`workflow_service.update_workflow`) with the accumulated definition, creating a new `WorkflowVersion` and resetting status to draft (re‑validate before publish). Optionally pass the chat transcript summary as `change_note`.

### Frontend — turn `AIRefineDrawer` into a chat editor
6. **New component `AIChatEditPanel.tsx`** (model the visual on `ChatPanel.tsx`, reuse message bubbles): a transcript + an input box, mounted on the detail page `/workflows/{id}/page.tsx` (button next to "Test chat"). Replaces/augments the single‑shot drawer.
7. **Per‑turn loop:**
   - On submit, POST `/workflows/{id}/augment` with `{message, current_definition: <editor store's current definition>, history: <prior turns>}`.
   - On response: append a user bubble (the instruction) and an assistant bubble rendering `changes[]` (e.g. "➕ added action `notify_slack`", "✏️ modified `scorer`").
   - **Apply** `proposed_definition` to the editor store so the **canvas live‑updates** (reuse the exact store mutation `refineWithAI` already performs in `WorkflowBuilder.tsx:284`). New nodes appear on the canvas immediately.
8. **Preview / accept / undo per turn:** because the editor store keeps undo history (InteractiveCanvas already has undo/redo stacks), each applied turn is one undoable step. Add an explicit "Undo last AI edit" affordance in the transcript. Optionally show the proposed definition as a **diff preview** before applying (use `changes[]` + node‑id diff) for a confirm step on destructive edits (removals).
9. **Save:** a "Save changes" button persists via the existing `updateWorkflow(id, { definition, change_note })` store method. Keep the draft‑reset / re‑publish behavior the backend enforces.
10. **Enablement:** like the current drawer, enable only when a saved `workflowId` exists (augment operates on a persisted id). The library list page (`/workflows/page.tsx`) can deep‑link "Edit with chat" → detail page with the panel open.

### Tests
11. Backend: extend `apps/api/tests/test_workflow_augment.py` — a multi‑turn case where turn 2 references a node added in turn 1 (verify it resolves and the id added in turn 1 is preserved). Assert `history` is honored and stable‑id preservation holds across turns.
12. Frontend: component test that a turn applies `proposed_definition` to the store and the canvas node count increases when a node is added; that "Undo" reverts one turn.

## 2.6 Edge cases & guardrails
- **Stable‑id drift:** the whole diff/canvas experience depends on the LLM echoing unchanged ids verbatim (rule #1 in `AUGMENT_SYSTEM_PROMPT`). Keep an automated check: any node present before *and* semantically unchanged after must keep its id; if the model renames, the diff explodes. Consider a post‑process that re‑maps obviously‑renamed nodes, or reject + retry.
- **Validation failures mid‑chat:** `augment()` can raise `WorkflowInterpretationError` after 2 attempts → show an inline assistant error bubble, keep the prior definition, let the user rephrase. Do not corrupt the working copy.
- **Destructive edits ("remove the agent"):** require an explicit confirm before applying removals (use the diff's "removed" list).
- **Concurrency:** if the workflow is edited elsewhere, base each turn on the editor store's current definition and warn on `PUT` version conflict.
- **Token budget:** cap `history` length + truncate large definitions; the definition itself is the bulk of the prompt.
- **Cost/telemetry:** each turn is an LLM call — already traced via Langfuse in `_call_llm`; surface a per‑session call count (mirrors the chat footer "tool calls" pattern noted in project memory).

---

## 3. Suggested sequencing

1. **Curl L2** (½–1 day) — provider + slug + interpreter vocab + tests. Self‑contained, unblocks "call any API" demos immediately.
2. **Chat‑edit backend** (1–1.5 days) — `history` on `AugmentRequest`, thread into `augment()`, prompt hardening, multi‑turn test.
3. **Chat‑edit frontend** (1.5–2 days) — `AIChatEditPanel`, live canvas apply, per‑turn diff, undo, save.
4. **Hardening** — SSRF allowlist for curl; destructive‑edit confirm; version‑conflict handling.

**Total: ~4–6 engineering days** for both features at production quality, because the heavy lifting (`HttpApiCall`, the provider/slot system, `augment` + `diff_definitions`, the chat UI primitives) already exists.

## 4. Files that will change (quick index)

**Curl (L2):**
- `apps/api/agents/native_providers.py` — `_curl_conn`, `_curl_tool`, `NativeProvider(id="curl")`.
- `apps/api/agents/native_tool_factory.py` — verify resolution; default no‑auth connection path.
- `apps/api/services/workflow_interpreter.py` — vocab in both prompts.
- `apps/web/src/components/workflow/PropertyInspector.tsx` — optional curl‑friendly `ActionFields`.
- `apps/api/tests/` — slug catalog + tool‑build + executor tests.

**Chat‑edit (Option A):**
- `apps/api/schemas/workflow.py` — `AugmentRequest.history`.
- `apps/api/services/workflow_interpreter.py` — `augment()` history threading + prompt hardening.
- `apps/web/src/components/workflow/AIChatEditPanel.tsx` *(new)* + mount in `apps/web/src/app/(app)/workflows/[id]/page.tsx`.
- `apps/web/src/stores/workflowStore.ts` — reuse `updateWorkflow`; transcript state if global.
- `apps/api/tests/test_workflow_augment.py` — multi‑turn coverage.

## 5. Open questions for product
1. **Curl:** do we need arbitrary‑host curl (SSRF surface) or is host‑allowlisted enough for v1?
2. **Curl:** distinct canvas node (L3) or tool‑slug/action (L2)? Recommend L2.
3. **Chat‑edit:** is per‑turn auto‑apply acceptable, or must every turn be preview‑then‑confirm? (Recommend auto‑apply with undo, confirm only on removals.)
4. **Chat‑edit:** do we need a server‑persisted edit transcript (Option B) for audit, or is client‑held history (Option A) fine for v1?
