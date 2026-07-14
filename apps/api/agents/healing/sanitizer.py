"""Deterministic cleanup of an LLM-produced workflow definition.

Runs *before* re-validation in the heal patch loop so the loop doesn't
oscillate on well-known LLM output quirks that a deterministic pass can fix
outright (audit finding G / plan §4.4).

Scope is intentionally conservative: it only removes/repairs *clearly invalid*
references and coerces obvious type slips. It never deletes a node — dropping a
node during a heal is a semantic decision, not a cleanup, and is left to the
diagnosis/patch step. Anything it can't safely fix is left for validation to
reject and the repair loop to re-prompt.

Operates on a plain ``dict`` (a ``WorkflowDefinition.model_dump()``) and returns
``(cleaned_dict, notes)``; the caller re-validates via
``WorkflowDefinition.model_validate``.
"""

from __future__ import annotations

from typing import Any

# Node kinds that are origins (no incoming dependencies) or satellites
# (invoked by a parent agent, never part of the top-level execution order).
_TRIGGER_KINDS = {"trigger"}


def sanitize_definition(definition: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a cleaned copy of ``definition`` plus a list of what changed."""
    notes: list[str] = []
    cleaned = dict(definition)
    nodes = cleaned.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        # Legacy agents-only shape has no node graph to clean.
        return cleaned, notes

    known_ids = {n.get("id") for n in nodes if isinstance(n, dict) and n.get("id")}
    satellite_ids = {
        n.get("id")
        for n in nodes
        if isinstance(n, dict) and n.get("parent_agent_id")
    }

    new_nodes: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            new_nodes.append(node)
            continue
        node = dict(node)
        nid = node.get("id")
        kind = node.get("kind")

        # 1. Triggers and satellites must not declare depends_on.
        if kind in _TRIGGER_KINDS and node.get("depends_on"):
            node["depends_on"] = []
            notes.append(f"cleared depends_on on trigger node {nid!r}")
        elif node.get("parent_agent_id") and node.get("depends_on"):
            node["depends_on"] = []
            notes.append(f"cleared depends_on on satellite node {nid!r}")

        # 2. depends_on: drop self-references and refs to unknown/satellite nodes.
        deps = node.get("depends_on")
        if isinstance(deps, list):
            kept = [
                d
                for d in deps
                if d in known_ids and d != nid and d not in satellite_ids
            ]
            if len(kept) != len(deps):
                node["depends_on"] = kept
                notes.append(f"pruned invalid depends_on refs on node {nid!r}")

        # 3. activate_on: drop keys that reference unknown nodes.
        act = node.get("activate_on")
        if isinstance(act, dict) and act:
            kept_act = {k: v for k, v in act.items() if k in known_ids}
            if len(kept_act) != len(act):
                node["activate_on"] = kept_act or None
                notes.append(f"pruned invalid activate_on refs on node {nid!r}")

        # 4. for_each: drop unknown body refs; blank an unknown items_from.
        if kind == "for_each":
            body = node.get("body")
            if isinstance(body, list):
                kept_body = [b for b in body if b in known_ids]
                if len(kept_body) != len(body):
                    node["body"] = kept_body
                    notes.append(f"pruned invalid for_each body refs on {nid!r}")

        # 5. condition: coerce a comma-string branches into a list, dedupe.
        if kind == "condition":
            branches = node.get("branches")
            if isinstance(branches, str):
                branches = [b.strip() for b in branches.split(",") if b.strip()]
                node["branches"] = branches
                notes.append(f"coerced string branches to a list on {nid!r}")
            if isinstance(branches, list):
                deduped = list(dict.fromkeys(branches))
                if len(deduped) != len(branches):
                    node["branches"] = deduped
                    notes.append(f"de-duplicated condition branches on {nid!r}")

        new_nodes.append(node)

    cleaned["nodes"] = new_nodes
    return cleaned, notes
