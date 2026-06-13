"""Derive the external integrations a workflow needs to run for real.

Pure (no DB) analysis of a :class:`WorkflowDefinition`: walks every node and
collects the providers that must be connected before a published run performs
real side effects — action providers, agents' explicit model providers, and
agent tool slugs that map to a connectable provider.

The connection *status* (and therefore the publish gate) is layered on top in
``WorkflowService`` since that needs the workspace's connections from the DB.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.native_providers import get_provider, resolve_provider_for_slug
from schemas.workflow import WorkflowDefinition

# Model providers the platform hosts itself — no per-workspace connection.
_PLATFORM_LLM: frozenset[str] = frozenset({"azure", ""})


@dataclass
class RequirementSpec:
    """One integration a workflow depends on, before status is resolved."""

    provider: str  # provider id, or a marker like "composio"
    name: str
    kind: str  # "action" | "tool" | "llm" | "saas"
    auth_type: str | None  # "oauth2" | "api_key" | "mcp_sse" | None
    connectable: bool  # we have a native catalog entry → can connect inline
    required: bool  # blocks publish when unconnected
    used_by: list[str] = field(default_factory=list)
    reason: str = ""


def derive_requirements(definition: WorkflowDefinition) -> list[RequirementSpec]:
    """Return the de-duplicated list of integrations ``definition`` needs."""
    acc: dict[str, RequirementSpec] = {}

    def add(
        *,
        provider: str,
        name: str,
        kind: str,
        auth_type: str | None,
        connectable: bool,
        required: bool,
        node_name: str,
        reason: str,
    ) -> None:
        key = provider.strip().lower()
        if not key:
            return
        existing = acc.get(key)
        if existing is not None:
            if node_name and node_name not in existing.used_by:
                existing.used_by.append(node_name)
            existing.required = existing.required or required
            return
        acc[key] = RequirementSpec(
            provider=key,
            name=name,
            kind=kind,
            auth_type=auth_type,
            connectable=connectable,
            required=required,
            used_by=[node_name] if node_name else [],
            reason=reason,
        )

    for node in definition.iter_nodes():
        kind = getattr(node, "kind", "")
        node_name = getattr(node, "name", None) or getattr(node, "id", "")

        if kind == "action":
            prov = (getattr(node, "provider", "") or "").strip().lower()
            if not prov:
                continue
            p = get_provider(prov)
            allow_dry = bool(getattr(node, "allow_dry_run", False))
            if p is not None:
                add(
                    provider=p.id,
                    name=p.name,
                    kind="action",
                    auth_type=p.auth_type,
                    connectable=True,
                    # A dry-run-capable action will echo instead of failing, so
                    # it's not a hard publish blocker.
                    required=not allow_dry,
                    node_name=node_name,
                    reason=f"Action “{node_name}” calls {p.name}.",
                )
            else:
                # Unknown provider — likely a Composio-routed SaaS call. Show it
                # but don't block publish (we can't connect it inline here).
                add(
                    provider=prov,
                    name=prov,
                    kind="saas",
                    auth_type=None,
                    connectable=False,
                    required=False,
                    node_name=node_name,
                    reason=f"Action “{node_name}” uses {prov} (connect in Integrations).",
                )

        elif kind == "agent":
            # ``chat_model`` may be a dict (loosely-typed schema field) or an
            # object depending on how the definition was parsed.
            chat_model = getattr(node, "chat_model", None)
            if isinstance(chat_model, dict):
                provider = (chat_model.get("provider") or "").strip().lower()
            else:
                provider = (getattr(chat_model, "provider", "") or "").strip().lower()
            if provider and provider not in _PLATFORM_LLM:
                p = get_provider(provider)
                if p is not None:
                    add(
                        provider=p.id,
                        name=p.name,
                        kind="llm",
                        auth_type=p.auth_type,
                        connectable=True,
                        required=True,
                        node_name=node_name,
                        reason=f"Agent “{node_name}” uses the {p.name} model provider.",
                    )
                else:
                    add(
                        provider=provider,
                        name=provider,
                        kind="llm",
                        auth_type="api_key",
                        connectable=False,
                        required=False,
                        node_name=node_name,
                        reason=f"Agent “{node_name}” uses the {provider} model provider.",
                    )

            for slug in getattr(node, "tools", []) or []:
                s = (slug or "").strip()
                if not s:
                    continue
                if s.upper().startswith("COMPOSIO_"):
                    add(
                        provider="composio",
                        name="Composio (SaaS tools)",
                        kind="saas",
                        auth_type="oauth2",
                        connectable=False,
                        required=False,
                        node_name=node_name,
                        reason=f"Agent “{node_name}” uses Composio-routed SaaS tools.",
                    )
                    continue
                p = resolve_provider_for_slug(s)
                if p is not None:
                    add(
                        provider=p.id,
                        name=p.name,
                        kind="tool",
                        auth_type=p.auth_type,
                        connectable=True,
                        required=True,
                        node_name=node_name,
                        reason=f"Agent “{node_name}” uses the {p.name} tool.",
                    )
                # Unresolved slug → built-in/internal tool (e.g. knowledge_base);
                # nothing to connect, so it's intentionally skipped.

    return list(acc.values())


__all__ = ["RequirementSpec", "derive_requirements"]
