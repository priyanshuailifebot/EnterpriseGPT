# Recruitment Workflow — Demo Guide

> **Audience:** you (the operator running the demo) + a manager watching.
> **Goal:** show the AI-native recruitment pipeline **published and working end-to-end**,
> plus the **self-healing** ("Diagnose & Heal") capability, using as few paid APIs as possible.
>
> Companion docs: [`RECRUITMENT_WORKFLOW_PLAN.md`](RECRUITMENT_WORKFLOW_PLAN.md) (architecture)
> and [`SELF_HEALING_PLAN.md`](SELF_HEALING_PLAN.md) (healing design).

---

## 0. TL;DR — what you're demoing

The recruitment pipeline is **six short workflows** correlated by `candidate_id`. Each one
does one bounded job and then ends; the next one is kicked off by an external event
(a form submit, a voice-call-ended webhook, a recruiter clicking a link, or a schedule).
This "event-boundary" design is why it behaves like a production n8n pipeline instead of one
fragile long-running graph.

Two capabilities make it adaptive rather than hardcoded:
- **Résumé screening** — Sourcing runs an LLM over every fetched candidate, scores each résumé
  against the JD on explicit criteria, and invites **only the shortlist** (default bar 70/100).
- **A per-role interview ladder** — Sourcing also asks an LLM to **design the sequence of rounds**
  for the role from the JD (2–4 rounds, each with a type, a focus, and a mode). Every round is
  **AI by default** (an AI voice interview) but any round can be a **human interviewer**; the last
  hiring-manager/offer round defaults to human. The chain **loops** through the ladder: score →
  human approval → next round, until the ladder is exhausted → **offer**.

| # | Workflow | Trigger | What it does | Hands off to |
|---|----------|---------|--------------|--------------|
| 1 | **HR Sourcing** (`hr-sourcing`) | Manual (JD + role) | Fetch candidates → **LLM screens résumés → shortlist** → **LLM designs the interview ladder** → email each shortlisted candidate a signed slot link | Web slot form → W2 |
| 2 | **HR Interview — Start Round** (`hr-interview-start`) | Webhook `hr-slot` | Candidate picked a slot → read the ladder, generate **that round's** questions → **AI voice call** *or* (human round) **book interviewer + email brief + `/hr/feedback` link** | Retell call-ended **or** feedback form → W3 |
| 3 | **HR Interview — Score & Review** (`hr-interview-scoring`) | Webhook `hr-scoring` | Score the round (transcript **or** human feedback) against the round's focus → email candidate → email recruiter approve/reject links | Recruiter click → W4 |
| 4 | **HR Interview — Decision** (`hr-decision`) | Webhook `hr-decision` | **Human gate.** Approve → **advance to the next round** (re-invite) or **extend the offer** if the ladder is done; Reject → mark `not_advanced` | Loops back to W2, or terminal |
| 5 | **HR Chaser** (`hr-chaser`) | Schedule `0 10 * * *` | Daily: email candidates who never picked a slot | (terminal) |
| 6 | **HR Ranking** (`hr-ranking`) | Schedule `0 18 * * 5` | Weekly Fri 18:00: stack-rank scored candidates | (terminal) |

**Two ways to demo, and you'll blend them:**

- **Preview / draft mode** — runs the *whole logic* with **zero paid APIs**: the ATS returns a
  built-in sample shortlist, agents call the LLM for real, and side-effecting actions (email,
  calendar, voice) are **simulated**. This is your safety net.
- **Published / live mode** — real side effects. Requires the workflow to be **Published** and
  the integrations it needs to be **Connected**. This is what makes it "published and working".

> **The one hard rule to remember:** the built-in **ATS sample shortlist only fires in draft/preview**.
> A *published* Sourcing workflow calls the real ATS URL you connect. See §4 for the free workaround.

---

## 1. Prerequisites (once, before the demo)

### 1.1 Bring up the stack

```bash
# from repo root
docker compose up -d postgres redis api web
# (only needed for the SCHEDULE + headless self-heal demos in §6/§7.4:)
# docker compose up -d api-worker
```

- **Web** (the UI you'll drive): http://localhost:3000
- **API**: http://localhost:8000  (docs at `/docs`)

> Port note: this repo's web dev server is **3000** (`next dev -p 3000`), even though the shared
> `CLAUDE.md` mentions 3001.

### 1.2 Environment — the only settings that matter for the demo

Everything automation-related ships **dormant (off) by default**. Copy `.env.example` → `.env`
and set the following. After any `.env` change, **restart `api` (and `api-worker`)** — settings are
cached per process.

| Var | For the demo set to | Why |
|-----|--------------------|-----|
| `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_API_KEY` | **your real values** | Required for agent scoring/ranking nodes **and** for Self-Heal (both make real LLM calls). Nothing "AI" works without this. |
| `APP_PUBLIC_URL` | `http://localhost:8000` (default) | Host for the recruiter **approve/reject** links. |
| `WEB_PUBLIC_URL` | `http://localhost:3000` (default) | Host for the candidate **slot-form** link. |
| `WORKFLOW_SCHEDULER_ENABLED` | `true` *(only for the schedule demo, §6)* | Master switch for cron triggers. Default `false`. |
| `RETELL_WEBHOOK_SECRET` | any string, e.g. `demo-retell-secret` *(only for a live voice call, §5)* | Empty = the voice callback returns 503. Not in `.env.example` — add it. |
| `AGENT_SELF_HEAL_MONITOR` | `true` *(only for headless auto-heal, §7.4)* | Master switch for the background healer. Default `false`. Interactive heal ignores it. |

> **Minimum to demo the core flow + interactive self-heal:** just Azure OpenAI creds + the two
> URL defaults. The scheduler, voice secret, and monitor flags are only for the optional sections.

### 1.3 Load the six templates into your workspace

There is **no auto-seed** — you instantiate each template once. In the web app:

1. Open **Library** (from the workflow editor header, or the templates page).
   Under category **HR** you'll see all six: `hr-sourcing`, `hr-interview-start`,
   `hr-interview-scoring`, `hr-decision`, `hr-chaser`, `hr-ranking`.
2. For each, click **Use as-is** (saves the baked v2 definition into your workspace as a new draft).

> Under the hood this is `GET /api/v1/workflows/templates` (read-only catalog) → save via
> `POST /api/v1/workflows`. **Instantiate all six in the *same* workspace** — they find each other
> by webhook **slug** + a signed workspace token, not by ID, so they must live together.

---

## 2. The publish gate (read this before you try to Publish)

Publishing is deliberately gated so you can't ship an untested/half-connected flow. `POST /publish`
returns **409** unless **both**:

1. **A successful test run exists** for the current version. → Run a **Test** (or a `/run`) first.
   A preview/demo run counts.
2. **Every required integration is Connected.** → Connect them in the **Integrations** panel; the
   **Test** button on each connector runs a real probe and must pass.

So the reliable per-workflow sequence is always: **Save → Test (preview) → Connect integrations → Publish.**

> If Publish is rejected, the UI toast says *"Publish failed — run a successful test first."* and,
> if the reason is a missing integration, it auto-opens the Integrations panel.

---

## 3. Recommended demo script (the happy path to show your manager)

This is the sequence that looks best live. It uses **preview mode to narrate the logic** and
**publishes with free/mock integrations** so the flow is genuinely live.

### Act 1 — "Here's the pipeline, and here's it thinking" (preview mode, no paid APIs)

1. Open **HR Sourcing** in the editor (`http://localhost:3000/workflows/{id}`).
   Point at the canvas: *trigger → fetch candidates (ATS) → store → for-each candidate → sign link → email invite.*
2. Click the green **Test** button (PlayCircle) in the canvas toolbar → run in **preview**.
   - The ATS node returns the **built-in sample shortlist** (Asha Rao, Vikram Singh) — **no ATS needed**.
   - Nodes light up with live status rings as the run streams.
   - Because it's a draft, the yellow **"Draft mode — actions run in preview"** banner shows: emails
     are simulated, but agent/LLM steps are real.
3. Repeat the **Test** on **HR Ranking** to show a *real agent* stack-ranking candidates from the
   LLM (this one has no external side effects, so it's a clean "AI at work" moment).

> Talking point: *"Every step you're seeing is the real engine — the only thing mocked in preview is
> the outbound side effect. Flip it to Published and the same graph does it for real."*

### Act 2 — "Now it's published and doing it for real"

Publish the workflows you want to show live (at minimum **Sourcing** + the three webhook workflows so
the chain resolves end-to-end). For each: **Save → Test → connect its integrations → Publish** (§2).
Use the free/mock integrations from **§4** so you don't need Darwinbox/paid ATS.

Then walk the candidate journey:

1. **Sourcing (published):** click **Run workflow** → `/workflows/{id}/run` → enter a **Job
   description** + **Role title** → **Run**. It pulls candidates (from your mock ATS, §4.1),
   **screens every résumé and keeps only the shortlist**, **designs the interview ladder** for the
   role, and emails each shortlisted candidate a real signed slot link
   (`http://localhost:3000/hr/slot?ctx=…`).
2. **Candidate books a slot:** open the slot link (from the sent email, or copy it from the run output).
   The public page **"Schedule your interview"** shows a time picker + language (English/Hindi/Tamil/
   Telugu/Marathi). Pick one → **Confirm slot** → *"Your interview slot is booked."*
   → this fires the **hr-slot** webhook → **Start Round** reads the ladder and runs the current round.
3. **The round runs:**
   - **AI round (default):** see §5 — a real Retell call, or simulate the call-ended event.
   - **Human round:** the **interviewer** gets an email brief with that round's questions + a
     **Submit feedback** button → the **`/hr/feedback`** page collects a rating + notes.
   Either way → fires **hr-scoring** → the round is scored against **its own focus**, the candidate
   gets a summary email, and **you (the recruiter) get an email with Approve / Reject links.**
4. **The human gate + the loop:** click **Approve** (or **Reject**) in the recruiter email. This is the
   headline governance story — **the system never advances or rejects a candidate on its own.**
   - Approve → **advances to the next round** (a fresh slot invite goes out and the cycle repeats),
     or — when the candidate has cleared the **last** round — sends the **offer**.
   - Reject → records `not_advanced`.
   Either way the candidate sees *"Thanks — your response has been recorded."*

> **Demo tip — no Retell needed:** set the ladder's rounds to `mode: "human"` and you can drive the
> **entire loop** (brief → `/hr/feedback` → per-round assessment → approve → next round → offer)
> with zero voice provider. This is exactly how the flow was verified end-to-end.

> **If you don't want to wire live voice** (most common for a first demo): publish W2/W3 and, at the
> point a call would complete, fire the scoring step yourself with a canned transcript (§5.2). The
> manager still sees scoring → recruiter email → human approve/reject working live.

---

## 4. External APIs — what each step needs, and the free/no-cost path

The recruitment templates reference five connector families. Here's each, whether it costs money,
and exactly what to do if you can't get the paid one.

### 4.1 ATS / résumé source  — e.g. **Darwinbox**  *(the one you asked about)*

- **Platform provider:** `ats` (bearer-HTTP). Tool: `ats_search_candidates` (POSTs `{jd, role, limit}`,
  expects a JSON list or `{data:[…]}` of `{candidate_id, name, email, phone}`). The generic
  `http_bearer` provider also ships `darwinbox_resume_search` / `darwinbox_candidate_get` slugs.
- **Cost:** Darwinbox is enterprise/paid — **no free tier.**
> **Screening needs résumé text.** Each candidate should carry a `resume` (or experience summary)
> field — that's what the LLM screen scores. Return a **pool of ~25+ candidates of varied quality**
> so the shortlist step visibly filters (e.g. 26 in → ~7 out). Two candidates with no résumé text
> won't demonstrate screening.

- **If you can't get it — three options, best first:**
  1. **Preview/Test mode (zero setup):** the built-in **demo stub** returns a couple of sample
     candidates automatically — fine to show the graph *runs*, but with no résumé text the screen
     step can't shortlist meaningfully. *Also: the stub does **not** fire once Published/live.*
  2. **Free mock endpoint (for a genuinely *published* run):** stand up a free static-JSON endpoint
     (e.g. **mocky.io**, **Beeceptor**, a **Pipedream** HTTP workflow, or **webhook.site**) that
     returns a candidate list **with résumés**, and connect it as the ATS **base_url**. Now a
     *published* Sourcing run really screens, shortlists, and emails candidates — no Darwinbox needed.
     Each item needs at least:

     ```json
     [
       { "candidate_id": "cand-asha", "name": "Asha Rao", "email": "you+asha@yourgmail.com",
         "phone": "+91-90000-00001",
         "resume": "6 years Field Sales Advisor, Pune FMCG. Carried a 1.2Cr quota, beat target 5/6 years. Fluent Marathi/Hindi/English." },
       { "candidate_id": "cand-dev", "name": "Dev Sharma", "email": "you+dev@yourgmail.com",
         "phone": "+91-90000-00002",
         "resume": "6 years backend software engineer (Java). No sales experience." }
     ]
     ```
     A bare JSON list or a `{ "data": [ … ] }` envelope both work. The repo's demo used a small local
     Python server returning ~26 such candidates (8 clear-fit, 6 borderline, 12 unrelated) so the
     screen shortlisted ~7.
     > Tip: use **your own** `you+alias@gmail.com` addresses so invites (and, for human rounds, the
     > interviewer brief at `you+interviewer@…`) land in your inbox and you can click through live.
  3. **Later, real Darwinbox:** just paste the real search endpoint URL + bearer token into the ATS
     connector. No workflow change.

### 4.2 Voice interview — **Retell** (or Vapi)

- **Platform provider:** `mcp` (register Retell/Vapi as an **MCP server** via its SSE URL; the
  server advertises `start_interview` / `get_interview_transcript` / `score_interview`). Plus the
  **Retell call-ended webhook** must point at `POST /api/v1/voice/retell/callback` (gated by
  `RETELL_WEBHOOK_SECRET`).
- **Cost:** Retell and Vapi are paid but **both offer free trial credits** — enough for a demo call.
- **If you can't get it — two no-voice options:**
  1. **Run the ladder in human mode** — set the rounds' `mode` to `human`. The interviewer gets a
     brief + `/hr/feedback` form; submitting it re-enters `hr-scoring` exactly like a transcript.
     This exercises the **whole loop** (including the multi-round advance → offer) with no voice
     provider at all, and every email is real. Recommended.
  2. **Simulate the call-ended event** for an AI round — drive `hr-scoring` yourself with a canned
     transcript (§5.2). You lose only the "phone actually rings" moment.

### 4.3 Email — **Gmail** (Google Workspace)

- **Platform provider:** `gmail` (OAuth). Tools: `gmail_send`, etc.
- **Cost:** **Free** with any Google account. Connect via the OAuth button in Integrations.
- **If you skip it:** in preview mode email is simulated anyway, so Act 1 needs nothing.

### 4.4 Calendar / scheduling — **Pipedream** (→ Calendly / Google Calendar)

- **Platform provider:** `pipedream` (OAuth). Tools: `pipedream_run_action`,
  `pipedream_calendly_create_event`. (There is **no** standalone Google-Calendar provider —
  calendar goes through Pipedream.)
- **Cost:** Pipedream has a **generous free tier**; Calendly free tier works.
- **If you skip it:** only the very last "book the HR round" step needs it. In preview it's simulated;
  or Approve → and just narrate "this books the round via Calendly" if you didn't connect it.

### 4.5 LLM — **Azure OpenAI** (primary)

- Used by the scoring/ranking **agents** and by **Self-Heal**. **Required, not optional.**
- Use your existing Azure OpenAI deployment (or the Anthropic key if configured). This is the one
  "paid" dependency you genuinely need — but you almost certainly already have it.

### Summary: cheapest viable demo

| Step | Paid? | Cheapest way to demo |
|------|-------|----------------------|
| ATS (Darwinbox) | Paid, no free tier | **Demo stub** (preview) or **free mock endpoint with résumés** (published, §4.1) |
| Voice (Retell) | Paid, free trial credits | **Human-mode rounds** (no voice at all), trial credits, or **simulate call-ended** (§5.2) |
| Email (Gmail) | **Free** | Connect Gmail (or simulated in preview) |
| Calendar (Pipedream/Calendly) | **Free tier** | Free tier (or simulated in preview) |
| LLM (Azure OpenAI) | Paid — **you already have it** | Your existing key |

**Net: you can run a fully *published* pipeline with $0 of new spend** — mock ATS + free Gmail +
free Pipedream + simulate the voice call. The only real prerequisite is the LLM key you already have.

---

## 5. Firing the voice / scoring step

### 5.1 Live (with Retell trial credits)
1. Register your Retell MCP server in **Integrations → MCP** (SSE URL + auth header).
2. Set `RETELL_WEBHOOK_SECRET=demo-retell-secret` in `.env`, restart `api`.
3. In Retell, point the **call-ended** webhook at
   `http://<your-api-host>/api/v1/voice/retell/callback` with header
   `X-Retell-Secret: demo-retell-secret`.
4. When the candidate books a slot, W2 places the call and registers it; when the call ends, Retell
   hits the callback → **hr-scoring** fires automatically.

### 5.2 Simulated (no Retell) — recommended for a first demo
After W2 has "placed the call", just POST the scoring trigger yourself with a canned transcript.
Because `hr-scoring` is a published webhook workflow, you can fire it by ID:

```bash
curl -X POST "http://localhost:8000/api/v1/workflows/{HR_SCORING_ID}/webhook/hr-scoring" \
  -H "Content-Type: application/json" \
  -d '{
        "candidate_id": "cand-001",
        "call_id": "demo-call-001",
        "transcript": "Interviewer: Tell me about your field sales experience... Candidate: I have 6 years..."
      }'
```

The scoring agent runs on the rubric, stores results, emails the candidate, and emails **you** the
Approve/Reject links. (If your MCP transcript tool isn't connected, pass the transcript inline as
above so the agent has something to score.)

> The callback is **one-shot** and **secret-gated**: an empty `RETELL_WEBHOOK_SECRET` returns 503,
> a wrong `X-Retell-Secret` returns 401, and each `call_id` route is consumed after one use.

---

## 6. (Optional) Schedule triggers — HR Chaser & HR Ranking

These two fire on cron, so they need the background worker running **and** the scheduler flag on:

1. `.env`: `WORKFLOW_SCHEDULER_ENABLED=true`, then `docker compose up -d api-worker` (runs
   `arq tasks.worker.WorkerSettings`). Restart `api-worker` after the flag change.
2. **Publish** the workflow (the dispatcher only considers **published** workflows with a schedule
   trigger).
3. For a live demo, temporarily set the cron to every minute (`* * * * *`) on the trigger node —
   the dispatcher checks each minute and fires within a **90-second** window (no catch-up).

> Under the hood: `dispatch_due_schedules` runs every minute, de-dupes each slot with a Redis
> `NX` key, and enqueues `run_workflow_execution` (which runs the flow as its owner). The daily/
> weekly crons in the templates (`0 10 * * *`, `0 18 * * 5`) are the production values.

---

## 7. Demoing Self-Healing on the recruitment workflow

This is the "the workflow fixes itself" story. There are two flavors. **Lead with the interactive
one — it always works, needs no flags, and is fully human-gated.**

### 7.1 What it is
**Diagnose & Heal** reads the workflow's definition (and any run history), asks the LLM to find
definition-level defects, proposes an **engine-validated patch**, and **stops** — it writes nothing
until *you* accept the fix on the canvas. It works even on a brand-new workflow with no run history
(it manufactures a demo run for evidence).

> Requirement: Azure OpenAI creds (§1.2). No `AGENT_SELF_HEAL_*` flag is needed for interactive heal —
> those flags only govern the *headless* monitor in §7.4.

### 7.2 Set up a defect to fix (do this before the demo)
The demo engine never "fails" a run at runtime, so the healer diagnoses the **definition**. Inject a
defect the LLM will flag as **auto-fixable**. Two reliable, recruitment-flavored choices:

- **(Recommended) Weak agent prompt** on **HR Ranking**: open the `rank` agent node and blank out /
  vaguen its instructions (e.g. just `"rank them"`). → diagnosed as a `prompt_issue`.
- **Broken condition** on **HR Decision**: change the `is_approved` expression from
  `$.start.decision == 'approve'` to reference a field that doesn't exist (e.g.
  `$.start.approved == true`). → diagnosed as an `edge_condition_issue` ("the approve branch can
  never fire").

Save the workflow after editing.

> Avoid breaking a workflow via a *missing integration/credential* — the healer marks those
> **not auto-fixable** on purpose, so it would only diagnose and not propose a patch (anticlimactic).
> Also avoid dangling `depends_on` — graph validation blocks the *save*.

### 7.3 Run the interactive demo (the money shot)
1. In the editor toolbar, click **Diagnose & Heal** (Stethoscope icon) → the drawer opens.
2. Click **Diagnose**. Watch the phases stream live:
   *Gathering run evidence… → Diagnosing… → Validating the fix against the engine… → Ready to review.*
3. It shows a **health pill** (healthy/degraded/broken), the **findings** (with severity +
   "auto-fixable"/"needs you" badges), and the **proposed change(s)**.
4. Click the green **Review fix on canvas** → a **"Proposed changes: N added, N edited, N removed"**
   banner appears on the canvas → click **Accept** → the fix is applied to the (still-unsaved) editor.
5. Click **Save**, then **Publish** to go live with the repaired version.

> Narration: *"It found the defect, wrote a schema-valid patch, and handed it to me for one-click
> review — nothing changed until I accepted. That's AI repair with a human in the loop."*
>
> **Save ≠ Publish ≠ Accept:** Accept only updates the in-editor definition; you still Save, then Publish.

### 7.4 (Advanced / optional) Autonomous headless healing
If you want to show the platform healing on its own, without a human:

- Set `AGENT_SELF_HEAL_MONITOR=true` and (for a fast demo) `AGENT_SELF_HEAL_INTERVAL_MINUTES=1`;
  run `api-worker`.
- Opt the workflow in: header **Auto-heal** menu → **Enable automatic healing** → policy **Safe** or
  **Autonomous** → Save (persists via `PUT /self-heal`).
- Policy semantics: **Safe** = auto-drafts a fix for a human to publish; **Autonomous** = applies,
  verifies (demo run + LLM judge), and re-publishes, rolling back if the judge fails.
- **Double-gate caveat:** the env ceiling `AGENT_SELF_HEAL_AUTO_APPLY` (default `safe`) caps the
  per-workflow policy — to actually auto-publish you must set `AGENT_SELF_HEAL_AUTO_APPLY=autonomous`
  **and** the workflow to Autonomous **and** it must already be published.
- The monitor only acts on **real (non-demo) failing/stuck runs in the last 60 min**, respects a
  6-hour per-workflow cooldown, and heals ≤3 workflows per pass.

Because of those guardrails, autonomous healing is hard to trigger reliably on stage. **Recommend
showing interactive heal (§7.3) and *describing* autonomous mode** using the Auto-heal menu as the
visual.

### 7.5 Show the audit trail
Every heal (interactive or headless) records an incident. Show them via
`GET /api/v1/workflows/{id}/incidents` (Redis-backed, last 50) — a nice "it keeps a record" closer.

---

## 8. Pre-demo checklist

- [ ] `docker compose up -d postgres redis api web` healthy (`+ api-worker` if §6/§7.4).
- [ ] `.env`: Azure OpenAI creds set; `APP_PUBLIC_URL` / `WEB_PUBLIC_URL` correct; `api` restarted.
- [ ] All **six** HR templates instantiated in **one** workspace (§1.3).
- [ ] Free **mock ATS** endpoint returning a **~26-candidate pool with `resume` text** (varied
      quality), connected as the ATS `base_url` (§4.1) — so screening visibly shortlists.
- [ ] Gmail connected (so invite/summary/interviewer/recruiter emails actually send), using
      `you+alias@` addresses (candidate, `+interviewer`, `+recruiter`).
- [ ] Each workflow you'll show live is **Published** (Save → Test → connect integrations → Publish).
- [ ] Decided round path: **human mode** (no voice — brief + `/hr/feedback`), live Retell trial, or
      simulate call-ended (§5.2). If live voice: `RETELL_WEBHOOK_SECRET` set + Retell webhook pointed
      at the callback.
- [ ] One workflow pre-loaded with a **self-heal defect** (§7.2), saved.
- [ ] Browser tabs open: editor, `/run` page (with JD + Role fields), a candidate `/hr/slot?ctx=…`
      link, and — for a human round — the interviewer `/hr/feedback?ctx=…` link.

## 9. Common gotchas (so nothing surprises you live)

- **ATS stub ≠ live.** Sample candidates only appear in preview/Test. A *published* Sourcing run
  calls your ATS `base_url` — use the mock (§4.1) or it'll error/return nothing.
- **Screening needs résumés.** The shortlist step scores a `resume` field. If the ATS returns
  candidates with no résumé text, everyone passes (or nobody does) — use a résumé-bearing pool (§4.1).
- **The ladder is stored per role.** Sourcing writes the LLM-designed ladder to `interview_plans`
  keyed by role; the round + decision workflows read it. Re-running Sourcing for the same role
  regenerates it. To force a specific ladder (e.g. all-`human` for a no-voice demo), seed that row.
- **Sourcing takes inputs.** The `/run` page shows **Job description** + **Role title** fields for
  Sourcing (a manual trigger with form fields). Fill both before Run.
- **Publish is gated** — you must run a successful Test first *and* connect required integrations,
  or you get a 409 / "run a successful test first" toast.
- **Everything automated is off by default** — scheduler, voice callback, and the self-heal monitor
  all ship dormant; flipping a flag also requires the `api-worker` container and an `api` restart.
- **Slug routing needs Published + same workspace.** The candidate/recruiter links resolve sibling
  workflows by webhook slug within the token's workspace, and those siblings must be **published**.
- **Save ≠ Publish ≠ Accept** in the editor (especially when accepting a heal fix).
- **`/hr/slot` needs the signed `ctx`** — opening it without the `?ctx=…` from an invite shows
  "invalid or expired". Always use a link produced by a Sourcing run.
- **Voice callback is one-shot + secret-gated** — re-register the call to retry; empty secret = 503.

---

*Built on the six-workflow event-boundary pipeline in `recruitment_templates.py`; healing in
`healing_service.py`; triggers in `routers/workflows.py` + `routers/voice.py`; background jobs in
`tasks/`. Everything ships dormant and safe — this guide is the switch-on runbook.*
