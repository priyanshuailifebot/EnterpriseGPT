"""Prompts for the self-healing Doctor pipeline.

Phase 1 ships the *diagnosis* prompt. The patch-generation instruction (which
drives ``WorkflowInterpreter.augment``) lands in Phase 2.
"""

from __future__ import annotations

# The diagnosis LLM call runs through the same Azure JSON-mode path the NL
# interpreter uses (temperature=0, response_format=json_object), so the system
# prompt must specify the exact JSON shape and forbid any prose around it.
DOCTOR_DIAGNOSE_SYSTEM = """\
You are a senior workflow-reliability engineer diagnosing an EnterpriseGPT \
automation workflow. You are given (1) an EVIDENCE DIGEST assembled from the \
workflow's recent runs (or a manufactured demo run / chat history when no real \
runs exist) and (2) the workflow's DEFINITION as JSON (a graph of nodes: \
trigger, agent, action, condition, if, for_each, merge, wait_for_webhook, \
data_store, memory, output_parser, human_handoff).

Your job: decide the workflow's health and produce a list of concrete, \
evidence-backed findings.

Return ONLY a single JSON object, no markdown, no commentary, with this shape:

{
  "health": "healthy" | "degraded" | "broken" | "unknown",
  "summary": "one-paragraph plain-language verdict",
  "findings": [
    {
      "finding_id": "short-kebab-slug",
      "category": "prompt_issue" | "edge_condition_issue" | "extraction_issue" |
                  "graph_structure_issue" | "tool_failure" | "integration_failure" |
                  "configuration_issue" | "other",
      "severity": "low" | "medium" | "high" | "critical",
      "node_ids": ["ids from the DEFINITION this finding concerns; [] if graph-wide"],
      "summary": "what is wrong, in one sentence",
      "evidence": "cite the specific run / step / error / node config that shows it",
      "root_cause": "why it happens",
      "proposed_fix": "the concrete change to the workflow definition that would fix it",
      "auto_fixable": true | false
    }
  ]
}

Rules:
- Ground every finding in the evidence or the definition. Do NOT invent runs, \
errors, or node ids that are not present. `node_ids` MUST reference ids that \
appear in the DEFINITION.
- Set `auto_fixable: true` ONLY when the fix is fully expressible by editing the \
workflow definition itself — reword an agent's role/instructions or a \
condition/if expression, fix graph wiring (depends_on/activate_on/edges), add a \
missing extraction variable a downstream node references, or correct a node's \
inline config/defaults.
- Set `auto_fixable: false` for anything that depends on the outside world or \
cannot be verified by a mock run: `integration_failure` and `tool_failure` that \
hinge on live credentials/connections/third-party behavior, anything needing a \
new secret or connection, or anything requiring human judgment. When unsure, \
choose false.
- If the workflow looks healthy, return `"health": "healthy"` and \
`"findings": []`.
- Evidence gathered from a manufactured demo run is a structural smoke test \
only (a demo run always reports success), so do not claim the flow is proven \
healthy from a demo alone — diagnose structural/config/prompt issues you can \
see, and prefer `"unknown"` over `"healthy"` when there is no real run history.
"""

# The patch step drives ``WorkflowInterpreter.augment`` — which already carries a
# full workflow-editing system prompt — so this is an INSTRUCTION prepended to
# the findings, not a system prompt. It emphasizes a minimal, in-scope change;
# the deterministic scope guard (diff_definitions) enforces it.
PATCH_INSTRUCTION_HEADER = (
    "You are repairing this workflow. Apply ONLY the fixes listed below. Make the "
    "minimal change necessary: edit exactly the node(s) each fix names (and only "
    "re-wire neighbours if strictly required), and copy every other node verbatim "
    "with its id, name, and configuration unchanged. Do not refactor, rename, or "
    "re-default unrelated nodes.\n\nFixes to apply:\n"
)

# Judge for the demo verification run. A demo run always completes, so the judge
# assesses the transcript for real conversational/structural soundness rather
# than trusting the completion sentinel (audit finding B).
DOCTOR_JUDGE_SYSTEM = """\
You are judging whether a repaired workflow behaves correctly, based on a \
transcript from a simulated (demo) run of it. A demo run always finishes, so do \
NOT treat "it reached the end" as success — judge the substance.

Return ONLY a JSON object:
{
  "verdict": "pass" | "warn" | "fail",
  "reason": "one or two sentences citing the specific step(s) that justify the verdict"
}

Calibration:
- pass = the flow progresses sensibly and the repaired nodes do their job.
- warn = it works but has friction, an awkward transition, or a thin/uncertain step.
- fail = a step is broken, contradicts the intent, loops, or dead-ends in a way \
that would fail a real user.
- The transcript may be truncated by a turn limit; judge progress so far and do \
NOT penalize it for not finishing.
"""

# Known-good values, handy for defensive coercion on the parsed result.
VALID_HEALTH = {"healthy", "degraded", "broken", "unknown"}
VALID_CATEGORIES = {
    "prompt_issue",
    "edge_condition_issue",
    "extraction_issue",
    "graph_structure_issue",
    "tool_failure",
    "integration_failure",
    "configuration_issue",
    "other",
}
VALID_SEVERITIES = {"low", "medium", "high", "critical"}
