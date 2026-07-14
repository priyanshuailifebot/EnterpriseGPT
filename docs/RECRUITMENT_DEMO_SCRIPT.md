# Recruitment Pipeline — Live Demo Script

> A presenter runbook for demoing the AI-native recruitment pipeline **end-to-end**
> (sourcing → slot → AI interview → scoring → human decision) **plus self-healing**,
> live, safely, to your own inbox. Everything below is already set up and verified
> working on this machine (`localhost`).
>
> Companion docs: [`RECRUITMENT_DEMO_GUIDE.md`](RECRUITMENT_DEMO_GUIDE.md) (concepts),
> [`RECRUITMENT_WORKFLOW_PLAN.md`](RECRUITMENT_WORKFLOW_PLAN.md) (architecture).

---

## 0. State check — already done (verify before presenting)

- **App up:** `docker compose up -d postgres redis api web` → web at `http://localhost:3000`.
- **All six HR workflows published** in *Priyanshu's Workspace*: `hr-sourcing`,
  `hr-interview-start`, `hr-interview-scoring`, `hr-decision`, `hr-chaser`, `hr-ranking`.
- **Gmail connected** (native OAuth) — sends for real.
- **Mock ATS connected** — a local self-addressed candidate source (background process on
  `:8899`; returns Asha → `startlordpriyanshu+asha@gmail.com`, Vikram → `+vikram`). If it
  died, restart: `python3 scratchpad/mock_ats.py &`.
- **Everything is self-addressed** — recruiter notices → `+recruiter@gmail.com`, candidate
  mail → `+asha`/`+vikram`. **No email can reach a real person.** Confirmed on the last walk.

**Safety line for the room:** *"Every email you'll see today lands in my own inbox — the
data source only emits `+alias` addresses of my Gmail."*

---

## 1. Opening — the pitch (30s)

> "This is an AI-native recruitment pipeline. It takes a role from sourcing to an offer
> decision — sourcing candidates, scheduling, running an AI voice screen, scoring it, and
> booking the next round — largely on its own, but with a person in control of the actual
> hiring decision. And when a step breaks, it can diagnose and repair itself. Let me show
> you the whole thing running live."

---

## 2. Act I — The pipeline (60s, in the editor)

Open **HR Sourcing** (`localhost:3000/workflows` → Detail). Pan the canvas.

> "It's not one giant automation — it's six small workflows, each doing one job and handing
> off to the next on a real event. Sourcing invites candidates; a candidate picking a slot
> starts the interview; the call ending triggers scoring; the recruiter's click drives the
> decision. Plus two scheduled jobs — a chaser and a weekly ranking."

**Why:** *"The gap between 'invite sent' and 'candidate replies' can be days. A single
long-running flow can't survive that. Splitting at each real-world event is what makes it
production-durable — the same way n8n models async."*

---

## 3. Act II — The live candidate journey (the walk)

> The chain hops on external events. In a demo you drive those events: run Sourcing, open
> the candidate link, (simulate the call), click the recruiter's Approve link.

### Scene 1 — Sourcing → invites *(watch them arrive)*
1. HR Sourcing → **Run workflow** → **Run**.
2. Timeline streams: **Fetch Candidates → Build Signed Slot Link ×2 → Send Interview Invitation ×2**, all *Completed*.
3. Switch to your inbox → **two invite emails** arrive (`+asha`, `+vikram`), each with a
   "book your slot" link.

> "A role goes in; real candidate invites go out — each with a personal, signed scheduling link."

### Scene 2 — Candidate books a slot → Interview Start
1. Open an invite's link → the public **"Schedule your interview"** page (`/hr/slot`).
2. Pick a time + language → **Confirm slot** → *"Your interview slot is booked."*
3. That fires the **`hr-slot`** webhook → **HR Interview — Start Call** runs and places the call.

> "The candidate self-schedules. That submission triggers the next workflow automatically —
> no recruiter in the loop yet."

### Scene 3 — The AI interview *(narrate; voice is stubbed here)*
> "An AI voice agent calls the candidate and runs a structured screen on an eight-point
> rubric — local-market knowledge, communication, objection handling, and so on."

**Reality note:** no Retell account is wired on this machine, so the call step runs as a
**dry-run** and scoring uses a canned transcript. With a Retell key, the call-ended webhook
fires scoring automatically. To keep the demo moving, trigger scoring yourself (Scene 4).

### Scene 4 — Scoring → the recruiter email
- **Live (with Retell):** the call-ended callback hits `/api/v1/voice/retell/callback` → scoring runs.
- **Demo (no Retell):** fire it with a canned transcript (Appendix A2). Either way:
  the transcript is scored, the candidate gets a summary, and **you (the recruiter) get an
  email with Approve / Reject buttons.**

> "When the interview finishes, it scores the conversation, emails the candidate a summary,
> and sends me the result with one-click Approve or Reject links."

### Scene 5 — The human decision *(the headline)*
1. Open the recruiter email → click **Approve** (a signed one-click link → `hr-decision`).
2. Confirmation: *"Thanks — your response has been recorded."*
3. Approve → books the HR round (calendar); Reject → records `not_advanced`.

> "This is the part that matters: everything up to here ran on its own, but the system
> **never** advances or rejects a candidate itself. That's always my explicit decision —
> captured with an audit trail. I click Approve, and it books the next round."

**Verified:** on the last live walk, all three hops returned `200 ok` and reached the booked
decision, with every email self-addressed.

---

## 4. Act III — Self-healing (the fix)

> Pre-seed a defect before the demo (or reuse the one already in HR Ranking).

**Setup (once):** open **HR Ranking**, click the **Stack Rank** agent node, and weaken its
instructions to something vague like *"Rank the candidates."* → **Save**. (A prompt issue —
the healer flags this as auto-fixable.)

**On stage:**
1. In the canvas toolbar, click **Diagnose & Heal** (stethoscope) → drawer opens.
2. Click **Diagnose**. Watch it stream: *Gathering evidence → Diagnosing → Validating the fix
   against the engine → Ready to review.*
3. It reports a **HIGH-severity, auto-fixable prompt issue** on the `rank` node, with a precise
   fix and a **Proposed change**.
4. Click **Review fix on canvas** → a *"Proposed changes: 1 edited"* banner appears → **Accept**.
5. The node's instructions are repaired → **Save** (→ new version).

> "Things break in production — a prompt gets sloppy, a condition gets mis-set. Instead of
> paging an engineer, I click Diagnose & Heal. It finds the fault, writes a real fix,
> validates it against the engine, and shows it to me — nothing changes until I accept.
> One click, and the workflow is repaired. AI that fixes itself, with a human holding the pen."

**Save ≠ Publish ≠ Accept:** Accept updates the editor; you still Save (and Publish to go live).

*(A recorded GIF of this exact flow exists: `egpt-self-heal-demo.gif`.)*

---

## 5. Closing (15s)

> "Sourcing to decision — mostly autonomous, always human-governed, self-healing when it
> matters. It runs on our own platform, no external orchestration tool. Happy to go deeper
> on any stage."

---

## Appendix A — Triggering the async hops

Most hops are real UI actions (run, click the invite link, click the recruiter link). The two
event-driven hops can be driven directly when you don't have live voice:

**A1 — Candidate slot (if not clicking the emailed link):**
`POST /api/v1/workflows/slug/hr-slot?ctx=<signed>` with `{"slot_iso": "...", "language": "en-IN"}`.

**A2 — Simulate the interview finishing (no Retell):**
`POST /api/v1/workflows/slug/hr-scoring?ctx=<signed>` with a `{"transcript": "..."}` body — runs
scoring and emails you the Approve/Reject links.

**A3 — Recruiter decision (if not clicking the emailed link):**
`GET /api/v1/workflows/slug/hr-decision?ctx=<signed>` where the signed context carries
`decision: "approve"` (or `"reject"`).

The `ctx` is a signed trigger token (workspace + candidate baked in). In the real flow these
links are minted by the pipeline and embedded in the emails — you just click them.

---

## Appendix B — What's real vs simulated on this machine

| Stage | On this demo box | With full credentials |
|---|---|---|
| ATS candidate search | **Real HTTP** to the local self-addressed mock | Your real ATS (Darwinbox, Ashby, …) |
| Candidate invite / summary / recruiter email | **Real Gmail send** (native OAuth) | Same |
| AI voice interview + transcript/scoring | **Dry-run** (no Retell) — use a canned transcript | Retell voice agent + real scoring |
| Calendar booking (Approve) | **Dry-run** (no Pipedream) | Pipedream → Calendly/Google Calendar |
| Human decision gate | **Real** (signed link → decision workflow) | Same |
| Self-healing | **Real** (Azure LLM diagnose + validated patch) | Same |

**Known gaps to fix for production (not blockers for the demo):**
- **`{{ start.X }}` trigger-field placeholders don't render** in the executor — the scoring
  step's candidate email was pointed at a self-address as a workaround. Fixing trigger-var
  resolution is the proper follow-up so candidate mail routes from the trigger context.
- Wire **Retell** (voice) and **Pipedream** (calendar) to make those two stages live.
