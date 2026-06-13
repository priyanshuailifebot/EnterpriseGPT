"""Map generated action nodes onto *connectable* native providers.

The interpreter (an LLM) emits free-form ``provider`` / ``action_slug`` values.
For the n8n-style "click the node → connect this integration" UX to work, each
action node must reference a provider that exists in the native catalog
(``agents/native_providers.py``) so the UI knows which credential form to show
and the executor knows how to run it.

This is a deterministic, conservative post-pass:
  * Clear SaaS verbs map to their catalog provider + canonical slug
    (email→gmail, message→slack, issue→jira, db→postgres, sms→twilio).
  * Internal / custom / unknown actions route to ``http_bearer`` (a generic,
    connectable REST provider) but KEEP their descriptive slug — so realistic
    demo records (TICKET-…, CUST-…) still fire and the node stays connectable.
  * Providers with no native equivalent (Google Sheets/Drive, Salesforce,
    Notion…) are left untouched — they remain Composio/dry-run, not forced.

It never invents params and only rewrites provider/slug, so it's safe across
arbitrary workflows.
"""

from __future__ import annotations

from agents.native_providers import list_providers
from schemas.workflow import WorkflowDefinition

_CATALOG_IDS = {p.id for p in list_providers()}

# SaaS providers with no native catalog entry — leave as-is (Composio/dry-run).
_PASSTHROUGH = {
    "googlesheets", "google_sheets", "sheets", "googledrive", "google_drive",
    "drive", "salesforce", "notion", "airtable", "hubspot", "zendesk",
    "darwinbox", "darwin_box",
}

_READ_VERBS = ("list", "fetch", "read", "get", "receive", "search", "query", "select", "find", "lookup")


def _map_provider_slug(provider: str, slug: str) -> tuple[str, str]:
    p = (provider or "").strip().lower()
    s = (slug or "").strip().lower()
    blob = f"{p} {s}"

    if "sendgrid" in p:
        return "sendgrid", "sendgrid_send"
    if "gmail" in p or "email" in blob or p == "mail":
        if any(v in s for v in ("list", "fetch", "read", "get", "receive", "search")):
            return "gmail", "gmail_list_messages"
        return "gmail", "gmail_send"
    if "slack" in blob:
        return "slack", "slack_post_message"
    if "jira" in blob:
        if any(v in s for v in ("search", "get", "find", "list")):
            return "jira", "jira_search_issues"
        return "jira", "jira_create_issue"
    if "twilio" in p or "sms" in blob or "whatsapp" in blob:
        return "twilio", "twilio_message_create"
    if p in {"postgres", "postgresql", "mysql", "sql", "database", "db"}:
        if any(v in s for v in _READ_VERBS):
            return "postgres", "sql_query"
        return "postgres", "sql_execute"

    # No native equivalent — don't force a remap.
    if p in _PASSTHROUGH:
        return provider, slug
    # Already a catalog provider — keep verbatim.
    if p in _CATALOG_IDS:
        return p, slug
    # Internal / custom / unknown → generic connectable REST. Keep the
    # descriptive slug so demo mocks (ticket/customer ids) still work.
    return "http_bearer", slug


def normalize_action_providers(definition: WorkflowDefinition) -> WorkflowDefinition:
    """Rewrite each action node's provider/slug onto a connectable provider.

    Mutates and returns ``definition`` (the validated model). No-op for any
    workflow without action nodes.
    """
    for node in definition.iter_nodes():
        if getattr(node, "kind", None) != "action":
            continue
        new_provider, new_slug = _map_provider_slug(
            getattr(node, "provider", ""), getattr(node, "action_slug", "")
        )
        if new_provider != node.provider:
            node.provider = new_provider
        if new_slug != node.action_slug:
            node.action_slug = new_slug
    return definition


__all__ = ["normalize_action_providers"]
