# Recruitment Demo — Video Recording Script

> **Use this to record a ~7–9 minute narrated screen video** (Loom, QuickTime, or OBS) of the HR
> recruitment pipeline: publishing, the real-world candidate journey, and self-healing.
> Each shot has **what to do on screen** + **exactly what to say**. Follow the setup in
> [`RECRUITMENT_DEMO_GUIDE.md`](RECRUITMENT_DEMO_GUIDE.md) once before you hit record.
>
> Recording tips: 1280×800 window, hide bookmarks/personal tabs, close notifications, zoom the
> browser to ~110% so nodes read on video. Speak slightly slower than feels natural. If you fluff a
> line, pause 2s and redo the sentence — easy to trim.

---

## Before you record (green-room checklist)

- [ ] Stack up: `docker compose up -d postgres redis api web` (+ `api-worker` only if you'll show schedules).
- [ ] `.env` has your **LLM key** (Azure OpenAI or Anthropic) — scoring + self-heal need it.
- [ ] All **six** HR templates instantiated in **one** workspace.
- [ ] A free **mock ATS** endpoint connected (returns the 2 sample candidates) using `you+alias@` emails.
- [ ] **Gmail connected** so invite/summary/recruiter emails actually send.
- [ ] One workflow pre-seeded with a **self-heal defect** (see Scene 6) — saved, *not yet healed*.
- [ ] Tabs ready: workflow editor, `/run` page, your email inbox, a candidate `/hr/slot?ctx=…` link.
- [ ] Do a silent dry-run once so timings feel natural.

**Total runtime target:** ~8 min. Scene budget below.

---

## Scene 1 — Cold open: the problem & the promise  *(0:00–0:40)*

**On screen:** The workflow editor open on **HR Sourcing**, the full 6-node graph visible.

> "This is an AI-native recruitment pipeline. It takes a role from sourcing all the way to an offer
> decision — placing screening interviews, scoring them, and scheduling next rounds — largely on its
> own. But it keeps a person in charge of the decisions that actually matter, and it can even repair
> itself when something breaks. Let me show you the whole thing, live."

**Cut cue:** slow pan across the canvas so the graph registers.

---

## Scene 2 — Anatomy: six small workflows  *(0:40–1:30)*

**On screen:** Open the **Library** / templates list showing the six `hr-*` workflows; hover each.

> "Instead of one giant fragile automation, it's six small workflows, each doing one job and handing
> off to the next: **Sourcing** finds candidates and invites them; **Start Call** places the AI
> interview once a candidate picks a time; **Score & Review** grades the conversation; **Decision** is
> the human approval step; and two background jobs — a **Chaser** that nudges people who haven't
> booked, and a weekly **Ranking**. Because they're separate and event-driven, one candidate's
> hiccup never stalls everyone else's."

**Cut cue:** end on the HR Sourcing tile.

---

## Scene 3 — Publishing an agent (governance moment)  *(1:30–2:40)*

**On screen:** HR Sourcing in the editor. Click the green **Test** button → let the preview run
stream (nodes light up). Then click **Publish** → show the **Draft → Published** pill flip.

> "Before anything goes live, I test it. Watch the run stream through each step in preview mode —
> this is the real engine; the only thing held back is the outbound email."

*(let the Test run finish)*

> "Now I publish it. Notice the platform won't let me go live until there's been a successful test
> **and** the integrations it needs are actually connected — so you can't accidentally ship something
> half-wired. And… it's live. The status flips to **Published**."

**If Publish is blocked:** narrate it as a feature — *"See, it's telling me to connect the ATS first —
that guardrail is intentional,"* connect it, then publish.

**Cut cue:** the emerald **Published** pill.

---

## Scene 4 — The real-world run: sourcing → invite  *(2:40–3:50)*

**On screen:** Click **Run workflow** → `/run` page → type a job description + role title → **Run**.
Show the streaming timeline complete. Then switch to your **email inbox** showing the invite that
arrived.

> "Here's what a real run looks like. I give it a role — say, 'Field Sales Advisor' — and run it.
> It pulls matching candidates from our applicant system and emails each of them a personal interview
> invitation."

*(switch to inbox)*

> "And here's that invite landing in a candidate's inbox, with a secure link to pick their own
> interview time. No recruiter had to touch any of this."

**Cut cue:** the invite email open, link visible.

---

## Scene 5 — Candidate journey: slot → AI interview → score  *(3:50–5:30)*

**On screen:** Click the invite link → the **`/hr/slot`** page ("Schedule your interview"). Pick a
time + language → **Confirm slot** → success screen. Then either (a) show the live voice call
completing, or (b) narrate that the call ran and jump to the scored result. Then open the
**recruiter email** with Approve / Reject links.

> "The candidate opens the link and picks a time — and their preferred language; we support English,
> Hindi, Tamil, Telugu and Marathi. They confirm…"

*(success screen)*

> "…and that instantly triggers the AI voice interview. An AI agent calls the candidate and runs a
> structured screening conversation on an eight-point rubric — local market knowledge, communication,
> objection handling, and so on."

*(jump to the scored result / recruiter email)*

> "When the call ends, the system fetches the transcript, scores it against that rubric, emails the
> candidate a summary, and — importantly — sends **me**, the recruiter, the result with two buttons:
> **Approve** or **Reject**."

**Cut cue:** the recruiter email, Approve/Reject links prominent.

---

## Scene 6 — The human gate (the headline)  *(5:30–6:10)*

**On screen:** Click **Approve** (or **Reject**) in the recruiter email → the confirmation page
("Thanks — your response has been recorded"). Optionally show the booked calendar event.

> "This is the part I care most about. The system does everything up to here on its own — but it
> **never** advances or rejects a candidate by itself. That's always an explicit human decision. I
> click Approve…"

*(confirmation)*

> "…and it books the next round automatically. If I'd clicked Reject, it would simply record that —
> no candidate is ever turned away by an algorithm without a person signing off."

**Cut cue:** the confirmation / booked event.

---

## Scene 7 — Self-healing (the wow)  *(6:10–7:40)*

**On screen:** Open the workflow you pre-broke. *(Recommended defect: on **HR Ranking**, blank the
`rank` step's instructions to something vague like "rank them"; **or** on **HR Decision**, change the
approval condition to reference a field that doesn't exist.)* Click **Diagnose & Heal** (stethoscope)
→ **Diagnose** → let the phases stream → **Review fix on canvas** → **Accept** → **Save**.

> "Now, things break in production — a prompt gets sloppy, a condition gets mis-set. Watch what
> happens. I've got a workflow here with a deliberately broken step. Instead of paging an engineer,
> I click **Diagnose & Heal**."

*(click Diagnose; let phases stream: Gathering evidence → Diagnosing → Validating → Ready)*

> "It reads the workflow, finds the fault, and — this is the key part — it drafts an actual fix and
> validates that the fix is structurally sound against the engine. It's not just telling me what's
> wrong; it's proposing the repair."

*(click Review fix on canvas → the diff banner appears)*

> "It shows me exactly what it wants to change — added, edited, removed — and nothing happens until I
> approve it. I review… and Accept."

*(Accept → Save)*

> "One click, and the workflow is repaired. AI that fixes itself, with a human still holding the pen."

**Cut cue:** the accepted diff / repaired canvas.

---

## Scene 8 — Close  *(7:40–8:10)*

**On screen:** Back to the full pipeline / the Published workflows list.

> "So: sourcing to decision, mostly autonomous, always human-governed, and self-healing when it
> matters. This runs today on our own platform — no external orchestration tool. Happy to go deeper
> on any part."

**Cut cue:** hold on the pipeline for 2s, stop recording.

---

## Fallbacks if something isn't wired live

| If you can't… | Do this instead (still looks real) |
|---|---|
| Place a real voice call (no Retell) | Narrate "the AI agent runs the call" over Scene 5, then trigger the scoring step with a canned transcript (Guide §5.2) so the scored result + recruiter email are genuine. |
| Connect a live ATS (Darwinbox is paid) | Use the free mock endpoint (Guide §4.1); the run and emails are real. |
| Show scheduled jobs (Chaser/Ranking) | Skip — just mention them in Scene 2. They're background jobs and don't film well. |
| Get self-heal to propose a fix | Make sure the defect is a **prompt** or **condition** flaw, not a missing integration (the healer intentionally won't auto-fix credential issues). |

## Editing notes

- Trim dead air between clicks; keep the stream animations (they're proof it's live).
- Add a title card: *"AI Recruitment Pipeline — live demo"* and lower-thirds for each scene name.
- Keep it under ~9 minutes; a manager will watch 8, not 15.
- Optional: end with the one-page briefing artifact on screen as an outro.
