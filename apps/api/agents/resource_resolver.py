"""Transparent name → ID resolution for Composio-backed action params.

Non-technical users describe resources by name ("ICICI Lombard Motor
Renewal") rather than by Google's 44-character spreadsheet IDs. This
module sits between the action runner and the MCP call: it walks the
action params, identifies values that look like names instead of IDs,
and resolves them via the appropriate Composio lookup tool
(``GOOGLEDRIVE_FIND_FILE``, ``SLACK_LIST_CHANNELS``, etc.).

Architecture
------------

* Each provider declares which param keys carry a "resource ID" and how
  to detect when the value is already an ID (a fast string heuristic).
* When a name is detected, the resolver runs the same discover-then-execute
  Composio flow the action_runner uses for unmatched action slugs:
  ``COMPOSIO_SEARCH_TOOLS`` to find the right lookup tool, then
  ``COMPOSIO_MULTI_EXECUTE_TOOL`` to invoke it.
* Resolved IDs are cached in Redis under a workspace-scoped key with a
  short TTL so repeated calls to the same sheet are free.
* The resolver is transparent to the action node: it just rewrites
  params. The action result carries a ``__resolved_params__`` diff so
  the UI can show what was resolved.

Coverage today
--------------

* Google Sheets — ``spreadsheet_id`` / ``spreadsheetId``
* Google Docs   — ``document_id`` / ``documentId``
* Google Slides — ``presentation_id`` / ``presentationId``
* Google Drive  — ``file_id`` / ``fileId``
* Google Calendar — ``calendar_id`` / ``calendarId``
* Slack         — ``channel`` / ``channel_id`` / ``channelId``
* Gmail         — ``label_id`` / ``labelId``

Add a new provider with a single ``register_resolver`` call.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import structlog
from redis.asyncio import Redis

from core.redis import get_redis as _get_redis
from egpt_mcp.mcp_tool_registry import MCPToolError, MCPToolRegistry

log = structlog.get_logger(__name__)

RESOLVER_CACHE_PREFIX = "egpt:resolver:"
RESOLVER_TTL_SECONDS = 300


# ---------------------------------------------------------------------------
# Heuristics for "is this value already an ID?"
# ---------------------------------------------------------------------------


def _is_google_drive_id(s: str) -> bool:
    """Google Drive / Sheets / Docs / Slides IDs are 25+ char alphanumeric
    strings with occasional ``-`` or ``_``, never spaces or dots."""
    if not s or " " in s or "." in s:
        return False
    if len(s) < 25:
        return False
    return all(c.isalnum() or c in "-_" for c in s)


def _is_slack_channel_id(s: str) -> bool:
    """Slack channel IDs: 9+ chars, start with C/G/D, uppercase alphanumeric."""
    return (
        len(s) >= 9
        and s[0] in "CGD"
        and s[1:].isalnum()
        and s == s.upper()
    )


def _is_gmail_label_id(s: str) -> bool:
    """Gmail label IDs: system labels (``INBOX``, ``SENT``, …) or ``Label_N``."""
    SYSTEM_LABELS = {
        "INBOX", "SENT", "DRAFT", "TRASH", "SPAM", "STARRED",
        "IMPORTANT", "UNREAD",
        "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
        "CATEGORY_UPDATES", "CATEGORY_FORUMS",
    }
    return s.upper() in SYSTEM_LABELS or s.startswith("Label_")


def _is_email_like(s: str) -> bool:
    """Google Calendar IDs are typically email-shaped (``you@gmail.com``)."""
    return "@" in s and "." in s.split("@", 1)[-1]


# ---------------------------------------------------------------------------
# Resolver registry
# ---------------------------------------------------------------------------


ResolveFn = Callable[["ResolverContext", str], Awaitable["str | None"]]


@dataclass
class ResolverContext:
    """Per-invocation context passed to each resolver function."""

    workspace_id: UUID | None
    redis: Redis
    registries: list[MCPToolRegistry]

    async def cache_get(self, key: str) -> str | None:
        if self.workspace_id is None:
            return None
        raw = await self.redis.get(f"{RESOLVER_CACHE_PREFIX}{self.workspace_id}:{key}")
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def cache_set(self, key: str, value: str) -> None:
        if self.workspace_id is None:
            return
        await self.redis.set(
            f"{RESOLVER_CACHE_PREFIX}{self.workspace_id}:{key}",
            value,
            ex=RESOLVER_TTL_SECONDS,
        )


@dataclass
class ResolverEntry:
    """One name-resolution rule for an action param."""

    param_keys: tuple[str, ...]
    is_id: Callable[[str], bool]
    resolver: ResolveFn


_REGISTRY: dict[str, list[ResolverEntry]] = {}

# Provider-scoped post-processors that get the *already-resolved* params and
# may rewrite them to handle cross-param dependencies. The classic example
# is Google Sheets: after the ``spreadsheet_id`` is resolved, the ``range``
# may still reference a tab name that doesn't exist in that spreadsheet —
# the post-processor fetches real tab names and substitutes the closest
# match. Each post-processor returns ``(new_params, resolution_log_delta)``.
PostProcessor = Callable[
    ["ResolverContext", dict[str, Any]],
    Awaitable[tuple[dict[str, Any], dict[str, dict[str, str]]]],
]
_POST_PROCESSORS: dict[str, list[PostProcessor]] = {}


def register_resolver(provider: str, entry: ResolverEntry) -> None:
    """Add a name→ID resolver for one provider."""
    _REGISTRY.setdefault(provider.lower(), []).append(entry)


def register_post_processor(provider: str, fn: PostProcessor) -> None:
    """Add a cross-param post-processor for one provider."""
    _POST_PROCESSORS.setdefault(provider.lower(), []).append(fn)


# ---------------------------------------------------------------------------
# Composio-backed lookup (SEARCH + EXECUTE)
# ---------------------------------------------------------------------------


async def _composio_lookup(
    ctx: ResolverContext,
    *,
    use_case: str,
    arguments: dict[str, Any],
    id_keys: tuple[str, ...],
    provider_hint: str,
) -> str | None:
    """Generic Composio name→ID lookup.

    Discovers the right tool slug via ``COMPOSIO_SEARCH_TOOLS``, executes
    it with the given arguments via ``COMPOSIO_MULTI_EXECUTE_TOOL``, and
    pulls the first value matching one of ``id_keys`` out of the response.
    """
    # Imported here to avoid a circular import (action_runner imports us).
    from agents.action_runner import _extract_best_slug

    for registry in ctx.registries:
        try:
            tools = await registry.list_tools()
        except MCPToolError:
            continue
        names = {str(t.get("name") or "") for t in tools}
        if (
            "COMPOSIO_SEARCH_TOOLS" not in names
            or "COMPOSIO_MULTI_EXECUTE_TOOL" not in names
        ):
            continue

        try:
            search_result = await registry.call_tool(
                db=None,
                tool_name="COMPOSIO_SEARCH_TOOLS",
                arguments={"queries": [{"use_case": use_case}]},
                execution_id=None,
            )
        except MCPToolError as exc:
            log.warning(
                "resolver.search_failed", use_case=use_case, error=str(exc),
            )
            continue

        slug = _extract_best_slug(search_result, provider_hint)
        if not slug:
            log.warning("resolver.no_slug", use_case=use_case, provider=provider_hint)
            continue

        try:
            exec_result = await registry.call_tool(
                db=None,
                tool_name="COMPOSIO_MULTI_EXECUTE_TOOL",
                arguments={
                    "tools": [{"tool_slug": slug, "arguments": arguments}],
                },
                execution_id=None,
            )
        except MCPToolError as exc:
            log.warning(
                "resolver.execute_failed",
                slug=slug, args=arguments, error=str(exc),
            )
            continue

        resolved = _extract_first_id(exec_result, id_keys)
        if resolved:
            return resolved

    return None


def _extract_first_id(call_result: Any, id_keys: tuple[str, ...]) -> str | None:
    """Walk a ``CallToolResult`` (including JSON-stringified ``content[].text``)
    and return the first value found under any of ``id_keys``."""
    candidates: list[str] = []

    def harvest(obj: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    harvest(parsed, depth + 1)
            return
        if isinstance(obj, dict):
            for key in id_keys:
                v = obj.get(key)
                if isinstance(v, str) and v.strip():
                    candidates.append(v.strip())
            for v in obj.values():
                harvest(v, depth + 1)
            return
        if isinstance(obj, list):
            for v in obj:
                harvest(v, depth + 1)

    harvest(call_result)
    # Prefer ID-shaped (no spaces, longer) values; sometimes the harvester
    # picks up display names from the same blob.
    candidates.sort(key=lambda s: (" " in s, -len(s)))
    return candidates[0] if candidates else None


# ---------------------------------------------------------------------------
# Per-provider resolvers
# ---------------------------------------------------------------------------


async def _resolve_drive_file(
    ctx: ResolverContext, name: str, *, mime_type: str | None,
) -> str | None:
    """Find a Google Drive file by name. Optionally filter by mime type
    (spreadsheet / document / presentation).

    Uses Composio's ``GOOGLEDRIVE_FIND_FILE`` which accepts a single ``q``
    parameter in Google Drive query syntax (same syntax as the Drive API).
    """
    cache_key = f"gdrive:{mime_type or '*'}:{name.lower()}"
    cached = await ctx.cache_get(cache_key)
    if cached:
        return cached

    # Build a Google Drive query string. Escape single quotes in the name.
    safe_name = name.replace("'", "\\'")
    q_parts = [f"name contains '{safe_name}'", "trashed = false"]
    if mime_type:
        q_parts.append(f"mimeType = '{mime_type}'")
    q = " and ".join(q_parts)

    # The "search files in google drive by name" use_case reliably returns
    # GOOGLEDRIVE_FIND_FILE as the top primary tool (verified against the
    # live MCP endpoint). The provider hint biases extraction toward Drive
    # slugs even if SEARCH returns multiple candidates.
    resolved = await _composio_lookup(
        ctx,
        use_case="search for files in google drive by name",
        arguments={"q": q},
        id_keys=("id", "file_id", "fileId", "spreadsheet_id", "spreadsheetId"),
        provider_hint="googledrive",
    )
    if resolved:
        await ctx.cache_set(cache_key, resolved)
    return resolved


def _drive_resolver_for(mime_type: str | None) -> ResolveFn:
    async def fn(ctx: ResolverContext, name: str) -> str | None:
        return await _resolve_drive_file(ctx, name, mime_type=mime_type)
    return fn


register_resolver(
    "googlesheets",
    ResolverEntry(
        param_keys=("spreadsheet_id", "spreadsheetId"),
        is_id=_is_google_drive_id,
        resolver=_drive_resolver_for("application/vnd.google-apps.spreadsheet"),
    ),
)
register_resolver(
    "googledocs",
    ResolverEntry(
        param_keys=("document_id", "documentId"),
        is_id=_is_google_drive_id,
        resolver=_drive_resolver_for("application/vnd.google-apps.document"),
    ),
)
register_resolver(
    "googleslides",
    ResolverEntry(
        param_keys=("presentation_id", "presentationId"),
        is_id=_is_google_drive_id,
        resolver=_drive_resolver_for("application/vnd.google-apps.presentation"),
    ),
)
register_resolver(
    "googledrive",
    ResolverEntry(
        param_keys=("file_id", "fileId"),
        is_id=_is_google_drive_id,
        resolver=_drive_resolver_for(None),
    ),
)


async def _resolve_slack_channel(ctx: ResolverContext, name: str) -> str | None:
    clean = name.lstrip("#").strip()
    cache_key = f"slack:channel:{clean.lower()}"
    cached = await ctx.cache_get(cache_key)
    if cached:
        return cached
    resolved = await _composio_lookup(
        ctx,
        use_case=f"find a slack channel named '{clean}'",
        arguments={"name": clean, "channel_name": clean},
        id_keys=("id", "channel_id", "channelId"),
        provider_hint="slack",
    )
    if resolved:
        await ctx.cache_set(cache_key, resolved)
    return resolved


register_resolver(
    "slack",
    ResolverEntry(
        param_keys=("channel", "channel_id", "channelId"),
        is_id=_is_slack_channel_id,
        resolver=_resolve_slack_channel,
    ),
)


async def _resolve_gmail_label(ctx: ResolverContext, name: str) -> str | None:
    cache_key = f"gmail:label:{name.lower()}"
    cached = await ctx.cache_get(cache_key)
    if cached:
        return cached
    resolved = await _composio_lookup(
        ctx,
        use_case=f"find a gmail label named '{name}'",
        arguments={"name": name, "label_name": name},
        id_keys=("id", "label_id", "labelId"),
        provider_hint="gmail",
    )
    if resolved:
        await ctx.cache_set(cache_key, resolved)
    return resolved


register_resolver(
    "gmail",
    ResolverEntry(
        param_keys=("label_id", "labelId"),
        is_id=_is_gmail_label_id,
        resolver=_resolve_gmail_label,
    ),
)


async def _resolve_google_calendar(ctx: ResolverContext, name: str) -> str | None:
    cache_key = f"gcal:cal:{name.lower()}"
    cached = await ctx.cache_get(cache_key)
    if cached:
        return cached
    resolved = await _composio_lookup(
        ctx,
        use_case=f"find a google calendar named '{name}'",
        arguments={"name": name, "summary": name},
        id_keys=("id", "calendar_id", "calendarId"),
        provider_hint="googlecalendar",
    )
    if resolved:
        await ctx.cache_set(cache_key, resolved)
    return resolved


register_resolver(
    "googlecalendar",
    ResolverEntry(
        param_keys=("calendar_id", "calendarId"),
        is_id=_is_email_like,
        resolver=_resolve_google_calendar,
    ),
)


# ---------------------------------------------------------------------------
# Post-processors — cross-param fixups after individual resolution
# ---------------------------------------------------------------------------


# Tab names commonly used for documentation rather than data — we skip these
# when auto-defaulting to "the first data tab" if the user's tab doesn't
# exist. Case-insensitive match against the tab name.
_DOC_TAB_NAMES = {
    "readme", "read me", "instructions", "how to use", "cover",
    "intro", "introduction", "help", "table of contents", "toc", "notes",
}

# Generic placeholder tabs templates often ship with — prefer real data tabs.
_GENERIC_TAB_NAMES = {
    "sheet1", "sheet2", "sheet 1", "sheet 2", "tab1", "tab 1", "data",
}

_DATA_TAB_KEYWORDS = ("customer", "master", "renewal", "policy", "contact")


def _pick_best_data_tab(available: list[str]) -> str | None:
    """Pick the most likely data-bearing tab, skipping docs/placeholders."""
    for kw in _DATA_TAB_KEYWORDS:
        for t in available:
            tl = t.lower()
            if kw in tl and tl not in _DOC_TAB_NAMES:
                return t
    for t in available:
        tl = t.lower()
        if tl not in _DOC_TAB_NAMES and tl not in _GENERIC_TAB_NAMES:
            return t
    for t in available:
        if t.lower() not in _DOC_TAB_NAMES:
            return t
    return available[0] if available else None


async def _fetch_sheet_tabs(
    ctx: ResolverContext, spreadsheet_id: str,
) -> list[str]:
    """Fetch the list of tab names for a Google Spreadsheet, cached."""
    cache_key = f"gsheets_tabs:{spreadsheet_id}"
    cached = await ctx.cache_get(cache_key)
    if cached:
        try:
            decoded = json.loads(cached)
            if isinstance(decoded, list):
                return [str(s) for s in decoded if isinstance(s, str)]
        except (TypeError, ValueError):
            pass

    # Call GOOGLESHEETS_GET_SHEET_NAMES via the meta-tool path. Reuse the
    # action_runner's slug extractor for consistency with how the rest of
    # the resolver discovers tools.
    from agents.action_runner import _extract_best_slug

    for registry in ctx.registries:
        try:
            tools = await registry.list_tools()
        except MCPToolError:
            continue
        names = {str(t.get("name") or "") for t in tools}
        if (
            "COMPOSIO_SEARCH_TOOLS" not in names
            or "COMPOSIO_MULTI_EXECUTE_TOOL" not in names
        ):
            continue
        try:
            search = await registry.call_tool(
                db=None,
                tool_name="COMPOSIO_SEARCH_TOOLS",
                arguments={
                    "queries": [
                        {"use_case": "list the tab names of a google spreadsheet"},
                    ],
                },
                execution_id=None,
            )
        except MCPToolError:
            continue
        slug = _extract_best_slug(search, "googlesheets")
        if not slug:
            continue
        try:
            result = await registry.call_tool(
                db=None,
                tool_name="COMPOSIO_MULTI_EXECUTE_TOOL",
                arguments={
                    "tools": [{
                        "tool_slug": slug,
                        "arguments": {"spreadsheet_id": spreadsheet_id},
                    }],
                },
                execution_id=None,
            )
        except MCPToolError:
            continue

        tabs = _extract_sheet_names(result)
        if tabs:
            await ctx.cache_set(cache_key, json.dumps(tabs))
            return tabs

    return []


def _extract_sheet_names(call_result: Any) -> list[str]:
    """Walk a CallToolResult and pull ``sheet_names`` / ``sheets`` arrays."""
    out: list[str] = []

    def harvest(obj: Any, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(obj, str):
            stripped = obj.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, (dict, list)):
                    harvest(parsed, depth + 1)
            return
        if isinstance(obj, dict):
            for key in ("sheet_names", "sheets", "tab_names", "tabs"):
                v = obj.get(key)
                if isinstance(v, list):
                    for s in v:
                        if isinstance(s, str) and s.strip():
                            out.append(s.strip())
            for v in obj.values():
                harvest(v, depth + 1)
            return
        if isinstance(obj, list):
            for v in obj:
                harvest(v, depth + 1)

    harvest(call_result)
    # Preserve order, dedup.
    seen: set[str] = set()
    deduped: list[str] = []
    for t in out:
        if t not in seen:
            seen.add(t)
            deduped.append(t)
    return deduped


def _pick_best_tab(requested: str, available: list[str]) -> str | None:
    """Choose the most likely tab name from ``available`` given a user's
    (possibly wrong) ``requested`` name. Strategy:

    1. Exact case-insensitive match.
    2. Substring match either direction.
    3. First tab whose name isn't a documentation/cover tab.
    4. Fallback: the first tab.
    """
    req_lower = requested.lower()
    for t in available:
        if t.lower() == req_lower:
            return t
    for t in available:
        if req_lower in t.lower() or t.lower() in req_lower:
            return t
    for t in available:
        if t.lower() not in _DOC_TAB_NAMES:
            return t
    return available[0] if available else None


async def _fix_sheets_range(
    ctx: ResolverContext, params: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Validate ``range``'s tab-name prefix against the real sheet; rewrite
    if the tab doesn't exist."""
    spreadsheet_id = (
        params.get("spreadsheet_id")
        or params.get("spreadsheetId")
    )
    rng = params.get("range")
    if not isinstance(spreadsheet_id, str) or not isinstance(rng, str):
        return params, {}
    if "!" not in rng:
        return params, {}
    if not _is_google_drive_id(spreadsheet_id):
        # Shouldn't happen — the spreadsheet_id resolver runs first — but
        # be defensive.
        return params, {}

    tab_name, cell_range = rng.split("!", 1)
    tab_name = tab_name.strip().strip("'")
    if not tab_name:
        return params, {}

    tabs = await _fetch_sheet_tabs(ctx, spreadsheet_id)
    if not tabs:
        return params, {}

    if tab_name in tabs:
        tl = tab_name.lower()
        if tl in _GENERIC_TAB_NAMES or tl in _DOC_TAB_NAMES:
            picked = _pick_best_data_tab(tabs)
            if picked and picked != tab_name:
                sheet_part = (
                    picked
                    if all(c.isalnum() or c == "_" for c in picked)
                    else f"'{picked}'"
                )
                new_range = f"{sheet_part}!{cell_range}"
                log.info(
                    "resolver.sheets_tab_replaced",
                    original=tab_name,
                    picked=picked,
                    available=tabs,
                )
                new_params = {**params, "range": new_range}
                delta = {"range": {"name": rng, "id": new_range}}
                return new_params, delta
        return params, {}  # already correct

    picked = _pick_best_tab(tab_name, tabs)
    if not picked or picked == tab_name:
        return params, {}

    # Wrap the new tab name in single quotes if it contains spaces or other
    # special characters; Sheets requires this in A1 notation.
    sheet_part = picked if all(c.isalnum() or c == "_" for c in picked) else f"'{picked}'"
    new_range = f"{sheet_part}!{cell_range}"

    log.info(
        "resolver.sheets_tab_fixed",
        original=tab_name, picked=picked, available=tabs,
    )

    new_params = {**params, "range": new_range}
    delta = {"range": {"name": rng, "id": new_range}}
    return new_params, delta


register_post_processor("googlesheets", _fix_sheets_range)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_action_params(
    *,
    provider_id: str,
    params: dict[str, Any],
    workspace_id: UUID | None,
    registries: list[MCPToolRegistry],
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Walk action params; swap name-shaped values for resolved IDs.

    Returns ``(new_params, resolved_log)`` where ``resolved_log`` records
    every successful resolution as ``{param_key: {"name": original, "id": resolved}}``.
    An empty log means nothing needed resolving (params were already IDs
    or no resolvers are registered for this provider).
    """
    key = (provider_id or "").lower()
    entries = _REGISTRY.get(key, [])
    post = _POST_PROCESSORS.get(key, [])
    if (not entries and not post) or not isinstance(params, dict) or not registries:
        return params, {}

    ctx = ResolverContext(
        workspace_id=workspace_id, redis=_get_redis(), registries=registries,
    )

    new_params = dict(params)
    resolved_log: dict[str, dict[str, str]] = {}

    for entry in entries:
        for pkey in entry.param_keys:
            if pkey not in new_params:
                continue
            val = new_params[pkey]
            if not isinstance(val, str) or not val.strip():
                continue
            if entry.is_id(val):
                continue
            try:
                resolved = await entry.resolver(ctx, val.strip())
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "resolver.failed",
                    provider=provider_id, key=pkey, name=val, error=str(exc),
                )
                continue
            if resolved:
                new_params[pkey] = resolved
                resolved_log[pkey] = {"name": val, "id": resolved}
                log.info(
                    "resolver.success",
                    provider=provider_id, key=pkey, name=val, resolved=resolved,
                )

    # Run provider-scoped post-processors after all name→ID resolutions.
    for fn in post:
        try:
            new_params, delta = await fn(ctx, new_params)
            resolved_log.update(delta)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "resolver.post_processor_failed",
                provider=provider_id, error=str(exc),
            )

    return new_params, resolved_log


__all__ = [
    "PostProcessor",
    "ResolverContext",
    "ResolverEntry",
    "register_post_processor",
    "register_resolver",
    "resolve_action_params",
]
