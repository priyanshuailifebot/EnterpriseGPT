"""Invoke an integration action by (provider, action_slug, params).

This is the runtime for the ``action`` node-kind — the deterministic,
non-LLM building block that mirrors n8n's per-integration nodes
(``Gmail: Send Message``, ``Google Calendar: Create Event``, etc.).

The runner does four things:

1. ``{{ <upstream_id>.<json_path> }}`` substitution against prior node
   outputs so the workflow author can reference upstream data without
   writing code.
2. Resolves the named provider's stored workspace connection (native
   first, Composio fallback).
3. Builds the matching Dynamiq tool node (``HttpApiCall`` for REST
   providers, ``SQLExecutor`` for Postgres, ``MCPServer`` for MCP
   servers, etc.) and invokes it with the params.
4. When no connection exists and ``allow_dry_run`` is set, returns a
   stub payload tagged ``__dry_run__: true`` so demo templates work
   without real credentials. This is what makes the template "demo-
   ready" out of the box.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from uuid import UUID

import structlog

from agents.native_providers import get_provider, resolve_provider_for_slug
from core.config import get_settings
from core.redis import get_redis
from egpt_mcp.mcp_tool_registry import MCPToolError, MCPToolRegistry
from models.native_connection import (
    NativeConnection,
    NativeConnectionStatus,
)
from services.native_connection_service import decode_config

# Lazily-built singleton — Settings/Redis are stable for the process lifetime,
# and constructing the registry is cheap.
_mcp_registry: MCPToolRegistry | None = None

# Composio's MCP returns a *preview-only* envelope (with the real payload
# saved to a sandbox file) when the inline response exceeds ~12k tokens.
# Capping unbounded sheet ranges here keeps the response inline so the
# downstream agent actually sees real rows.  ~200 rows × ~18 cols of CSV
# fits comfortably under the inline limit; 500 did not in practice.
_DEFAULT_SHEET_ROW_CAP = 200
_MIN_SHEET_ROW_CAP = 25

# Maps workflow-author slugs (provider, action) → Composio's real tool name.
# Used to short-circuit Strategy 1 (exact match) so we never need MULTI_EXECUTE,
# which only works on Composio's "Tool Router" server type.
_DIRECT_TOOL_ALIASES: dict[tuple[str, str], str] = {
    ("googlesheets", "read_range"):       "GOOGLESHEETS_VALUES_GET",
    ("googlesheets", "values_get"):       "GOOGLESHEETS_VALUES_GET",
    ("googlesheets", "batch_get"):        "GOOGLESHEETS_BATCH_GET",
    ("googlesheets", "batch_read_range"): "GOOGLESHEETS_BATCH_GET",
    ("gmail",        "fetch"):            "GMAIL_FETCH_EMAILS",
    ("gmail",        "fetch_emails"):     "GMAIL_FETCH_EMAILS",
    ("gmail",        "send"):             "GMAIL_SEND_EMAIL",
    ("gmail",        "send_email"):       "GMAIL_SEND_EMAIL",
    ("gmail",        "send_message"):     "GMAIL_SEND_EMAIL",
    ("googledrive",  "find_file"):        "GOOGLEDRIVE_FIND_FILE",
    ("googledrive",  "search"):           "GOOGLEDRIVE_FIND_FILE",
    ("sendgrid",     "send"):             "SENDGRID_SEND_EMAIL",
    ("sendgrid",     "send_email"):       "SENDGRID_SEND_EMAIL",
}


def _get_mcp_registry() -> MCPToolRegistry | None:
    """Return the process-wide MCP registry, or ``None`` if not configured."""
    global _mcp_registry
    settings = get_settings()
    key = (settings.COMPOSIO_MCP_API_KEY or "").strip()
    url = (settings.COMPOSIO_MCP_URL or "").strip()
    if not key or not url:
        return None
    if _mcp_registry is None:
        try:
            _mcp_registry = MCPToolRegistry(settings, get_redis())
        except Exception:  # noqa: BLE001 — never crash callers on init failure
            log.warning("mcp.registry.init_failed", exc_info=True)
            return None
    return _mcp_registry


async def _try_invoke_via_mcp(
    *,
    provider_id: str,
    action_slug: str,
    params: dict[str, Any],
    registries: list[MCPToolRegistry] | None = None,
    workspace_id: UUID | None = None,
    db: Any | None = None,
) -> dict[str, Any] | None:
    """Attempt to invoke ``action_slug`` through any MCP endpoint that
    advertises it. Two strategies in order:

    1. **Exact name match** — the MCP server advertises a tool whose name
       equals ``action_slug`` (case-insensitive). Call it directly.
    2. **Composio meta-tool routing** — when the server exposes the
       ``COMPOSIO_SEARCH_TOOLS`` + ``COMPOSIO_MULTI_EXECUTE_TOOL`` pair,
       discover the real tool slug at runtime via SEARCH and invoke via
       MULTI_EXECUTE. This is how static action nodes like
       ``googlesheets.read_range`` route through Composio MCP without the
       interpreter having to know the exact Composio slug ahead of time.

    ``registries`` can be passed pre-built (the action runner's pre-flight
    resolver builds them once and re-uses); otherwise we fetch them here.
    Returns the standard action-result dict on success, or ``None`` when
    no MCP endpoint can handle the action.
    """
    if registries is None:
        registries = await _collect_mcp_registries(workspace_id=workspace_id, db=db)
    if not registries:
        return None

    target_slug = (action_slug or "").strip()
    if not target_slug:
        return None
    aliased = _DIRECT_TOOL_ALIASES.get(
        ((provider_id or "").strip().lower(), target_slug.lower())
    )
    if aliased:
        target_slug = aliased
    target_upper = target_slug.upper()

    for registry in registries:
        try:
            tools = await registry.list_tools()
        except MCPToolError:
            continue

        tool_names = {str(t.get("name") or "") for t in tools}

        # Strategy 1 — exact match.
        if target_slug in tool_names or target_upper in tool_names:
            real_name = target_slug if target_slug in tool_names else target_upper
            try:
                output = await registry.call_tool(
                    db=None,
                    tool_name=real_name,
                    arguments=params or {},
                    execution_id=None,
                )
            except MCPToolError as exc:
                raise ActionInvocationError(f"MCP `{real_name}` failed: {exc}") from exc
            return {
                "__provider__": provider_id or _infer_provider_from_slug(real_name),
                "__action__": real_name,
                "__via__": "mcp",
                "__dry_run__": False,
                "data": _safe_jsonable(output),
            }

        # Strategy 2 — Composio meta-tool routing.
        if (
            "COMPOSIO_SEARCH_TOOLS" in tool_names
            and "COMPOSIO_MULTI_EXECUTE_TOOL" in tool_names
            and provider_id
        ):
            try:
                return await _invoke_via_composio_meta(
                    registry=registry,
                    provider_id=provider_id,
                    action_slug=target_slug,
                    params=params or {},
                )
            except MCPToolError as exc:
                # Composio couldn't route/execute (e.g. the tool-router session
                # isn't available, or no matching tool). Don't hard-fail the
                # action — fall through to the native provider path below, which
                # uses a configured native connection or a clean dry-run when
                # unconnected. Prevents a misconfigured Composio from aborting
                # every action it can't handle.
                log.warning(
                    "action_runner.composio_meta.routing_failed_fallback",
                    provider=provider_id,
                    action=target_slug,
                    error=str(exc)[:200],
                )
                return None

    return None


async def _invoke_via_composio_meta(
    *,
    registry: MCPToolRegistry,
    provider_id: str,
    action_slug: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Two-step Composio invocation: SEARCH for the canonical tool slug,
    then MULTI_EXECUTE it with the user's params.

    Schema reference (from Composio's MCP tool list):
    * ``COMPOSIO_SEARCH_TOOLS`` takes ``{queries: [{use_case, known_fields?}]}``
      and returns a list of tool candidates.
    * ``COMPOSIO_MULTI_EXECUTE_TOOL`` takes ``{tools: [{tool_slug, arguments, account?}]}``
      and executes them in parallel.
    """
    use_case = f"{provider_id} {action_slug.replace('_', ' ')}".strip()
    log.info(
        "action_runner.composio_meta.search",
        provider=provider_id,
        action=action_slug,
        use_case=use_case,
    )

    search_result = await registry.call_tool(
        db=None,
        tool_name="COMPOSIO_SEARCH_TOOLS",
        arguments={"queries": [{"use_case": use_case}]},
        execution_id=None,
    )

    slug = _extract_best_slug(search_result, provider_id, action_slug=action_slug)
    if not slug:
        # Last-resort fallback: try a constructed slug. Composio will
        # reject if it's wrong and the error will surface cleanly.
        slug = f"{provider_id.upper()}_{action_slug.upper()}"
        log.warning(
            "action_runner.composio_meta.no_search_match",
            provider=provider_id,
            action=action_slug,
            fallback_slug=slug,
        )

    log.info(
        "action_runner.composio_meta.execute",
        provider=provider_id,
        action=action_slug,
        resolved_slug=slug,
    )

    exec_result = await _composio_execute_with_preview_retry(
        registry=registry,
        slug=slug,
        provider_id=provider_id,
        action_slug=action_slug,
        params=params or {},
    )

    return {
        "__provider__": provider_id,
        "__action__": action_slug,
        "__resolved_slug__": slug,
        "__via__": "composio_mcp_meta",
        "__dry_run__": False,
        "data": _safe_jsonable(exec_result),
    }


def _result_is_preview_only(result: Any) -> bool:
    """Detect Composio's truncated ``data_preview`` envelope.

    Composio swaps the inline ``data`` payload for ``data_preview``
    (with a ``remote_file_info`` block pointing at a sandbox file)
    whenever the response exceeds the inline size limit. The agent
    cannot read that sandbox file, so we treat it as a soft failure
    and retry with a smaller range.
    """

    def walk(node: Any, depth: int = 0) -> bool:
        if depth > 8:
            return False
        if isinstance(node, dict):
            if "data_preview" in node and "data" not in node:
                return True
            for v in node.values():
                if walk(v, depth + 1):
                    return True
            return False
        if isinstance(node, list):
            return any(walk(v, depth + 1) for v in node)
        if isinstance(node, str):
            stripped = node.strip()
            if stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(stripped)
                except (TypeError, ValueError):
                    return False
                return walk(parsed, depth + 1)
        return False

    return walk(result)


def _halve_sheet_range(rng: str, *, floor: int = _MIN_SHEET_ROW_CAP) -> tuple[str, int]:
    """Halve the row count on a bounded A1 range. Returns ``(new_range, new_max)``.

    ``Customer_Master!A1:R200`` → ``Customer_Master!A1:R100``
    ``A1:Z50``                  → ``A1:Z25``
    """
    rng = (rng or "").strip()
    if not rng or ":" not in rng:
        return rng, 0
    tab, _, body = rng.partition("!")
    if not body:
        body, tab = tab, ""
    left, _, right = body.partition(":")

    def _split(t: str) -> tuple[str, str]:
        col, num = "", ""
        for ch in t:
            if ch.isalpha():
                col += ch
            else:
                num += ch
        return col, num

    l_col, l_num = _split(left)
    r_col, r_num = _split(right)
    if not r_col or not r_num.isdigit():
        return rng, 0
    new_max = max(floor, int(r_num) // 2)
    new_right = f"{r_col}{new_max}"
    new_body = f"{l_col}{l_num or '1'}:{new_right}"
    return (f"{tab}!{new_body}" if tab else new_body), new_max


async def _composio_execute_with_preview_retry(
    *,
    registry: MCPToolRegistry,
    slug: str,
    provider_id: str,
    action_slug: str,
    params: dict[str, Any],
    max_attempts: int = 4,
) -> Any:
    """Invoke ``COMPOSIO_MULTI_EXECUTE_TOOL`` with auto-retry on truncated previews.

    When Composio returns a ``data_preview`` envelope (because the inline
    response exceeded ~12k tokens), we halve the Google Sheets row range
    and retry. This is critical for downstream agents: a preview is
    practically useless to them — they can't open the sandbox file and
    will hallucinate placeholders to try to "fix" the call.
    """
    attempt_params = dict(params)
    last_result: Any = None
    for attempt in range(max_attempts):
        normalised = _normalize_composio_arguments(
            provider_id, slug, action_slug, attempt_params
        )
        result = await registry.call_tool(
            db=None,
            tool_name="COMPOSIO_MULTI_EXECUTE_TOOL",
            arguments={
                "tools": [
                    {
                        "tool_slug": slug,
                        "arguments": normalised,
                    }
                ]
            },
            execution_id=None,
        )
        last_result = result
        is_sheet_read = (
            provider_id.lower() == "googlesheets" and "VALUES" in slug.upper()
        )
        if not is_sheet_read or not _result_is_preview_only(result):
            return result

        new_range, new_max = _halve_sheet_range(
            str(attempt_params.get("range") or "")
        )
        if not new_range or new_max <= _MIN_SHEET_ROW_CAP and attempt > 0:
            log.warning(
                "action_runner.composio_meta.preview_floor",
                provider=provider_id,
                action=action_slug,
                attempt=attempt,
            )
            return result
        log.info(
            "action_runner.composio_meta.preview_retry",
            provider=provider_id,
            action=action_slug,
            attempt=attempt + 1,
            new_range=new_range,
            new_row_cap=new_max,
        )
        attempt_params["range"] = new_range
    return last_result


def _extract_best_slug(
    search_result: Any,
    provider_id: str,
    *,
    action_slug: str | None = None,
) -> str | None:
    """Extract the best-matching Composio tool slug from a SEARCH_TOOLS response.

    The MCP CallToolResult wraps the actual payload as a JSON-stringified
    blob inside ``content[0].text``. The decoded payload looks like:

        {
          "data": {
            "results": [
              {
                "use_case": "...",
                "primary_tool_slugs": ["GOOGLESHEETS_VALUES_GET", ...],
                "related_tool_slugs": [...],
                "toolkits": ["googlesheets", "gmail"]
              }
            ],
            "tool_schemas": {...}
          }
        }

    Strategy: parse the content payload, then pull the first slug from
    ``primary_tool_slugs`` whose prefix matches the requested provider.
    Fall back to ``related_tool_slugs`` then to any slug found anywhere.
    """
    prefix = provider_id.upper().replace("-", "_").replace(" ", "_")

    # Walk MCP content blocks and JSON-decode any embedded text payload.
    decoded_payloads: list[Any] = []

    def harvest_text_payloads(obj: Any) -> None:
        if isinstance(obj, dict):
            if obj.get("type") == "text" and isinstance(obj.get("text"), str):
                txt = obj["text"]
                try:
                    decoded_payloads.append(json.loads(txt))
                except (TypeError, ValueError):
                    pass
            for v in obj.values():
                harvest_text_payloads(v)
        elif isinstance(obj, list):
            for v in obj:
                harvest_text_payloads(v)

    harvest_text_payloads(search_result)

    primary: list[str] = []
    related: list[str] = []

    def collect(obj: Any) -> None:
        if isinstance(obj, dict):
            ps = obj.get("primary_tool_slugs")
            if isinstance(ps, list):
                primary.extend([str(s) for s in ps if isinstance(s, str)])
            rs = obj.get("related_tool_slugs")
            if isinstance(rs, list):
                related.extend([str(s) for s in rs if isinstance(s, str)])
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)

    for payload in decoded_payloads:
        collect(payload)
    # Also walk the raw structure in case the response shape changes.
    collect(search_result)

    def looks_like_slug(s: str) -> bool:
        return (
            s.isupper()
            and "_" in s
            and len(s) >= 4
            and all(c.isalnum() or c == "_" for c in s)
        )

    # Prefer primary slugs that start with the provider prefix.
    ordered = primary + related
    slug_hint = (action_slug or "").lower()
    if slug_hint and "read" in slug_hint:
        for s in ordered:
            if "VALUES_GET" in s.upper():
                return s
    for s in primary:
        if s.startswith(prefix + "_"):
            return s
    if primary:
        return primary[0]
    for s in related:
        if s.startswith(prefix + "_"):
            return s
    if related:
        return related[0]

    # Last-resort generic walk for any uppercase slug-looking string.
    generic: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, str) and looks_like_slug(obj):
            generic.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(search_result)
    for s in generic:
        if s.startswith(prefix + "_"):
            return s
    return generic[0] if generic else None


def _bound_sheet_range(rng: str, *, max_rows: int = _DEFAULT_SHEET_ROW_CAP) -> str:
    """Add an explicit row limit to an A1 range that's open-ended.

    ``Customer_Master!A:R``  →  ``Customer_Master!A1:R500``
    ``A:Z``                  →  ``A1:Z500``
    ``Sheet1!A1:R500``       →  unchanged
    """
    rng = (rng or "").strip()
    if not rng or ":" not in rng:
        return rng
    tab, _, body = rng.partition("!")
    if not body:
        body, tab = tab, ""
    if ":" not in body:
        return rng
    left, _, right = body.partition(":")

    def _split(token: str) -> tuple[str, str]:
        col, num = "", ""
        for ch in token:
            if ch.isalpha():
                col += ch
            else:
                num += ch
        return col, num

    l_col, l_num = _split(left)
    r_col, r_num = _split(right)
    if not l_col or not r_col:
        return rng
    if l_num and r_num:
        return rng
    new_left = f"{l_col}{l_num or '1'}"
    new_right = f"{r_col}{r_num or max_rows}"
    body = f"{new_left}:{new_right}"
    return f"{tab}!{body}" if tab else body


def _normalize_composio_arguments(
    provider_id: str,
    resolved_slug: str,
    action_slug: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Rewrite action params to match Composio tool schemas.

    The workflow interpreter emits provider-catalog shapes (``range``,
    ``spreadsheetId``); Composio tools often expect different key names
    (``ranges``, ``spreadsheet_id``). Normalizing here keeps every workflow
    working without per-template patches.
    """
    out = dict(params or {})
    prov = (provider_id or "").lower()
    slug_up = (resolved_slug or "").upper()

    if prov == "googlesheets" or "GOOGLESHEETS" in slug_up:
        sid = out.get("spreadsheetId") or out.get("spreadsheet_id")
        if sid and not out.get("spreadsheet_id"):
            out["spreadsheet_id"] = sid
        rng = out.get("range")
        if isinstance(rng, str) and rng.strip():
            rng = _bound_sheet_range(rng.strip())
            if "BATCH" in slug_up and "ranges" not in out:
                out["ranges"] = [rng]
            elif "VALUES" in slug_up:
                out["range"] = rng
            else:
                out["range"] = rng
        if isinstance(out.get("ranges"), list):
            out["ranges"] = [
                _bound_sheet_range(r) if isinstance(r, str) else r
                for r in out["ranges"]
            ]
    return out


# Demo candidate pool for draft/preview ATS runs — varied-quality résumés so the
# résumé-screening step visibly shortlists (~7 of 26) and the ladder is
# role-shaped. (slug, name, résumé) tuples; emails are example.com (preview never
# sends). Roughly: 8 clear-fit sales, 6 borderline, 12 unrelated.
_ATS_DEMO_POOL: list[tuple[str, str, str]] = [
    # --- strong: relevant field sales, quota, territory/language ---
    ("asha", "Asha Rao", "6 years Field Sales Advisor at a Pune FMCG distributor. Carried a 1.2Cr quota, beat target 5 of 6 years. 40+ retail accounts across Maharashtra. Marathi, Hindi, English."),
    ("vikram", "Vikram Singh", "8 years B2B field sales in industrial equipment, Delhi-NCR. 110-130% of quota, President's Club twice. Strong consultative selling and objection handling."),
    ("meera", "Meera Nair", "5 years channel sales for a telecom, Bangalore. Grew territory revenue 35% YoY, onboarded 60 dealers. Kannada, Tamil, English."),
    ("rohit", "Rohit Deshmukh", "7 years pharma field sales rep, Western India. Exceeded quota 4 years running, top 5% nationally. Deep local network. Marathi, Hindi."),
    ("fatima", "Fatima Sheikh", "4 years FMCG territory sales officer, Hyderabad. 122% avg attainment, expanded distribution to 90 outlets. Telugu, Urdu, English."),
    ("arjun", "Arjun Menon", "9 years B2C field sales in consumer durables, Kochi. 25-store territory, beat quota 7 of 9 years. Malayalam, Tamil, English."),
    ("neha", "Neha Kulkarni", "5 years insurance field advisor, Pune. MDRT qualifier 3 times, 130% of premium target. Consultative needs-based selling. Marathi, Hindi."),
    ("sameer", "Sameer Patel", "6 years agri-inputs field sales, Gujarat. Grew dealer base 40%, quota attainment 115%. Gujarati, Hindi. Farmer engagement + demos."),
    # --- borderline: some sales exposure but gaps ---
    ("priya", "Priya Iyer", "2 years inside sales (phone, not field) for a SaaS firm, Chennai. Hit 105% of quota. No field experience yet. Tamil, English."),
    ("kabir", "Kabir Khan", "3 years retail store manager, then 1 year sales executive. Limited territory ownership. Hindi, English."),
    ("divya", "Divya Reddy", "Marketing associate 4 years with occasional field activation. No direct quota ownership. Telugu, English."),
    ("aman", "Aman Gupta", "1 year FMCG field sales trainee, still ramping. Early quota attainment ~90%. Hindi, English."),
    ("sanya", "Sanya Malhotra", "3 years customer success manager (renewals/upsell) at a fintech. Revenue-adjacent, not field sales. English, Hindi."),
    ("irfan", "Irfan Ansari", "Real-estate broker 5 years (commission-based). Strong negotiation, no structured quota. Hindi, Urdu, English."),
    # --- weak: no relevant sales / wrong domain ---
    ("dev", "Dev Sharma", "6 years backend software engineer (Java, microservices). No sales experience."),
    ("tara", "Tara Bose", "Registered nurse, 8 years in a Kolkata hospital. No sales background. Bengali, Hindi."),
    ("mohan", "Mohan Kumar", "Accountant, 10 years, CA. Bookkeeping and audit. No customer-facing sales role."),
    ("lata", "Lata Verma", "Primary school teacher 12 years. Excellent communication but no sales or quota experience."),
    ("nikhil", "Nikhil Jain", "Fresh graduate, B.Sc Physics. No work experience."),
    ("gita", "Gita Pillai", "Data analyst, 4 years, SQL/Python dashboards. No field or sales experience."),
    ("raj", "Raj Malhotra", "Warehouse operations supervisor, 7 years logistics. No selling role."),
    ("shreya", "Shreya Das", "Graphic designer, 5 years freelance. Creative portfolio, no sales."),
    ("imran", "Imran Qureshi", "Mechanical engineer in manufacturing QA, 6 years. No sales experience."),
    ("pooja", "Pooja Shetty", "HR generalist, 5 years recruiting and payroll. No revenue/quota role."),
    ("vivek", "Vivek Rao", "Chef and kitchen manager, 9 years hospitality. No sales background."),
    ("anita", "Anita Joseph", "Content writer and editor, 6 years. Strong English, no sales role."),
]


def _ats_demo_stub(params: dict[str, Any]) -> dict[str, Any]:
    """Sample candidate pool for draft/preview runs of the ATS search action.

    Shape matches the ``ats`` connector contract (list under ``data``). Includes
    a ``resume`` field and a varied-quality pool so the sourcing template's
    résumé-screening step shortlists realistically without a live ATS. Preview
    runs simulate email, so the ``example.com`` addresses never send.
    """
    role = str(params.get("role") or "Field Sales Advisor")
    samples = [
        {
            "candidate_id": f"cand-{slug}",
            "name": name,
            "email": f"{slug}@example.com",
            "phone": "+91-90000-00000",
            "resume": resume,
            "role": role,
        }
        for slug, name, resume in _ATS_DEMO_POOL
    ]
    return {
        "__provider__": "ats",
        "__action__": "ats_search_candidates",
        "__dry_run__": True,
        "data": samples,
    }


def _gmail_send_native(params: dict[str, Any], *, access_token: str) -> dict[str, Any]:
    """Send an email via the Gmail REST API using a native OAuth connection.

    Builds an RFC-822 MIME message and POSTs it to ``users/me/messages/send``.
    Used when a native ``gmail`` connection exists, instead of the Composio
    meta-tool. Accepts ``to``/``recipient_email``, ``cc``, ``bcc``, ``subject``
    and an HTML body (``html_body``/``html``) or plain body (``body``/``text``).
    """
    import base64
    from email.mime.text import MIMEText

    import httpx

    p = params or {}
    to = str(p.get("to") or p.get("recipient_email") or "").strip()
    cc = str(p.get("cc") or "").strip()
    bcc = str(p.get("bcc") or "").strip()
    subject = str(p.get("subject") or "").strip()
    html = p.get("html_body") or p.get("html")
    if html:
        mime = MIMEText(str(html), "html", "utf-8")
    else:
        mime = MIMEText(str(p.get("body") or p.get("text") or ""), "plain", "utf-8")
    if to:
        mime["To"] = to
    if cc:
        mime["Cc"] = cc
    if bcc:
        mime["Bcc"] = bcc
    mime["Subject"] = subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()

    with httpx.Client(timeout=30.0) as client:
        resp = client.post(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            json={"raw": raw},
        )
    if resp.status_code >= 400:
        raise ActionInvocationError(
            f"gmail.gmail_send HTTP {resp.status_code}: {resp.text[:200]}"
        )
    sent = resp.json() if resp.content else {}
    return {
        "__provider__": "gmail",
        "__action__": "gmail_send",
        "__dry_run__": False,
        "data": {
            "id": sent.get("id"),
            "threadId": sent.get("threadId"),
            "to": to,
            "subject": subject,
        },
    }


def _sign_link(params: dict[str, Any], *, workspace_id: UUID | None = None) -> dict[str, Any]:
    """Build a signed trigger link. ``params``:
      * ``context`` — dict baked into the signed token (e.g. candidate_id).
      * ``path``    — path appended to the app base URL (e.g. the slug-based
                      trigger route ``/api/v1/workflows/slug/{trigger_slug}`` or
                      a frontend form page).
      * ``ttl_seconds`` — optional link lifetime (default 7 days).
    The workspace id is auto-baked into the token under ``__ws__`` so the
    slug-based trigger route can resolve the sibling workflow without the caller
    knowing its id. Returns ``data.url`` and ``data.token``.
    """
    from core.config import get_settings
    from core.security import sign_trigger_context

    context = dict(params.get("context")) if isinstance(params.get("context"), dict) else {}
    if workspace_id is not None and "__ws__" not in context:
        context["__ws__"] = str(workspace_id)
    path = str(params.get("path") or "")
    ttl = int(params.get("ttl_seconds") or 7 * 24 * 3600)
    token = sign_trigger_context(context, ttl_seconds=ttl)
    settings = get_settings()
    # base="web" → the frontend (for user-facing form pages); default → the API.
    if str(params.get("base") or "").lower() == "web":
        base = (getattr(settings, "WEB_PUBLIC_URL", "") or "").rstrip("/")
    else:
        base = (getattr(settings, "APP_PUBLIC_URL", "") or "").rstrip("/")
    sep = "&" if "?" in path else "?"
    url = f"{base}{path}{sep}ctx={token}" if base else f"{path}{sep}ctx={token}"
    return {
        "__provider__": "internal",
        "__action__": "sign_link",
        "__dry_run__": False,
        "data": {"url": url, "token": token},
    }


_VOICE_ROUTE_KEY = "egpt:voice:route:{call_id}"
_VOICE_ROUTE_TTL = 6 * 3600


async def _register_voice_route(
    params: dict[str, Any], *, workspace_id: UUID | None, live: bool
) -> dict[str, Any]:
    """Persist ``call_id → {workspace_id, target_slug, ctx}`` so the Retell
    call-ended callback knows which workflow (by webhook-trigger slug) to fire
    for this candidate. Preview runs skip the write (no real call placed)."""
    import json as _json

    call_id = str(params.get("call_id") or "").strip()
    target_slug = str(params.get("target_slug") or "").strip()
    ctx = params.get("context") if isinstance(params.get("context"), dict) else {}
    if not call_id or not target_slug:
        return {"ok": False, "error": "call_id and target_slug are required"}
    if not live or workspace_id is None:
        return {"__dry_run__": True, "data": {"call_id": call_id, "target_slug": target_slug}}
    from core.redis import get_redis

    record = {"workspace_id": str(workspace_id), "target_slug": target_slug, "ctx": ctx}
    await get_redis().set(
        _VOICE_ROUTE_KEY.format(call_id=call_id), _json.dumps(record), ex=_VOICE_ROUTE_TTL
    )
    return {"data": {"registered": True, "call_id": call_id}}


def _coerce_rounds(val: Any) -> list[dict[str, Any]]:
    """Parse a ladder value (JSON string or list) into a list of round dicts."""
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except json.JSONDecodeError:
            from services.output_parser_service import extract_json_loose

            val = extract_json_loose(val)
    if isinstance(val, dict) and isinstance(val.get("rounds"), list):
        val = val["rounds"]
    return [r for r in val if isinstance(r, dict)] if isinstance(val, list) else []


def _coerce_index(val: Any) -> int:
    try:
        return int(str(val).strip())
    except (TypeError, ValueError):
        return 0


async def _llm_json(params: dict[str, Any]) -> dict[str, Any]:
    """Direct structured-JSON LLM call (bypasses the ReAct agent, which narrates
    prose instead of emitting strict JSON). Used by sourcing's résumé-screen and
    ladder-design steps, which must return machine-parseable JSON. ``params``:
    ``system`` (instructions) + ``input`` (the data). Returns ``{data: <obj>}``."""
    from core.config import get_settings
    from openai import AsyncAzureOpenAI

    s = get_settings()
    ep = (s.AZURE_OPENAI_ENDPOINT or "").strip().rstrip("/")
    key = (s.AZURE_OPENAI_API_KEY or "").strip()
    if not ep or not key:
        return {"data": {}, "__error__": "azure_openai credentials missing"}
    deployment = (
        s.AZURE_OPENAI_WORKFLOW_DEPLOYMENT
        or s.AZURE_OPENAI_DEPLOYMENT
        or getattr(s, "AZURE_OPENAI_DEFAULT_MODEL", "")
    )
    client = AsyncAzureOpenAI(
        azure_endpoint=ep, api_key=key, api_version=s.AZURE_OPENAI_API_VERSION
    )
    comp = await client.chat.completions.create(
        model=deployment,
        temperature=0.0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": str(params.get("system") or "")},
            {"role": "user", "content": str(params.get("input") or "")},
        ],
    )
    txt = comp.choices[0].message.content or "{}"
    try:
        data = json.loads(txt)
    except json.JSONDecodeError:
        from services.output_parser_service import extract_json_loose

        data = extract_json_loose(txt) or {}
    return {"data": data}


def _hr_pick_round(params: dict[str, Any]) -> dict[str, Any]:
    """Deterministically select ladder[round_index]. Pure function (no LLM) so
    control-flow (AI/human mode, round name) is always reliably structured."""
    rounds = _coerce_rounds(params.get("ladder"))
    idx = _coerce_index(params.get("round_index"))
    rnd = rounds[idx] if 0 <= idx < len(rounds) else {}
    return {
        "data": {
            "name": rnd.get("name", ""),
            "type": rnd.get("type", ""),
            "mode": (rnd.get("mode") or "ai"),
            "focus": rnd.get("focus", ""),
            "index": idx,
            "total": len(rounds),
        }
    }


def _hr_advance(params: dict[str, Any]) -> dict[str, Any]:
    """Deterministically compute the next rung of the ladder. Pure function."""
    rounds = _coerce_rounds(params.get("ladder"))
    idx = _coerce_index(params.get("round_index"))
    nxt = idx + 1
    has_next = 0 <= nxt < len(rounds)
    return {
        "data": {
            "next_index": nxt,
            "has_next": has_next,
            "next_name": (rounds[nxt].get("name", "") if has_next else ""),
        }
    }


def _hr_stack_rank(params: dict[str, Any]) -> dict[str, Any]:
    """Deterministically stack-rank interview results by numeric score. Pure
    function (no LLM — the ReAct agent narrates prose). Input ``rows`` is the
    data_store query result (``{rows:[{key,data}]}`` or a bare list). Returns
    ``{data: {ranking: [{rank, candidate_id, name, role_title, score, status}]}}``.
    """
    rows = params.get("rows")
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except json.JSONDecodeError:
            from services.output_parser_service import extract_json_loose

            rows = extract_json_loose(rows)
    if isinstance(rows, dict) and isinstance(rows.get("rows"), list):
        rows = rows["rows"]
    items: list[dict[str, Any]] = []
    for r in rows if isinstance(rows, list) else []:
        d = r.get("data", r) if isinstance(r, dict) else {}
        if not isinstance(d, dict):
            continue
        try:
            score = float(d.get("score"))
        except (TypeError, ValueError):
            score = -1.0
        items.append(
            {
                "candidate_id": d.get("candidate_id"),
                "name": d.get("name") or d.get("candidate_id"),
                "role_title": d.get("role_title"),
                "score": score if score >= 0 else None,
                "status": d.get("status"),
                "current_round_name": d.get("current_round_name") or d.get("round_name"),
            }
        )
    items.sort(key=lambda x: (x["score"] if x["score"] is not None else -1), reverse=True)
    for i, it in enumerate(items, start=1):
        it["rank"] = i
    return {"data": {"ranking": items, "count": len(items)}}


async def _fire_workflow(
    params: dict[str, Any], *, workspace_id: UUID | None
) -> dict[str, Any]:
    """Fire a sibling workflow by trigger slug with a signed ctx — used to
    SIMULATE the voice-call-ended callback for AI rounds when no voice provider
    is connected. ``params``: ``target_slug`` + ``context`` (baked into the token)
    + ``payload`` (posted body, e.g. the simulated transcript)."""
    import httpx

    from core.config import get_settings
    from core.security import sign_trigger_context

    target = str(params.get("target_slug") or "").strip()
    if not target:
        return {"data": {}, "__error__": "target_slug required"}
    ctx = dict(params.get("context") or {})
    if workspace_id is not None and "__ws__" not in ctx:
        ctx["__ws__"] = str(workspace_id)
    payload = params.get("payload") if isinstance(params.get("payload"), dict) else {}
    token = sign_trigger_context(ctx)
    s = get_settings()
    base = (getattr(s, "APP_PUBLIC_URL", "") or "http://localhost:8000").rstrip("/")
    url = f"{base}/api/v1/workflows/slug/{target}?ctx={token}"
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(url, json=payload)
        return {"data": {"fired": target, "status_code": resp.status_code}}
    except Exception as exc:  # pragma: no cover - network/timeout
        return {"data": {"fired": target}, "__error__": f"fire_workflow failed: {exc}"}


def _generate_pdf(params: dict[str, Any]) -> dict[str, Any]:
    """Render markdown/plain text to a PDF payload (base64 envelope)."""
    from services.pdf_service import render_pdf_result

    content = str(
        params.get("content")
        or params.get("markdown")
        or params.get("text")
        or params.get("body")
        or ""
    ).strip()
    title = str(params.get("title") or params.get("filename") or "Report").strip()
    return render_pdf_result(title=title, content=content)


async def _collect_mcp_registries(
    *,
    workspace_id: UUID | None,
    db: Any | None,
) -> list[MCPToolRegistry]:
    """Build the ordered list of MCP registries to try for this invocation."""
    out: list[MCPToolRegistry] = []
    settings = get_settings()
    redis = get_redis()

    if workspace_id is not None and db is not None:
        try:
            # Import inside the function so the module loads even before
            # the mcp_servers migration has run.
            from services.mcp_server_service import (
                list_servers,
                to_server_config,
            )

            rows = await list_servers(db, workspace_id)
            for row in rows:
                try:
                    out.append(
                        MCPToolRegistry(
                            settings, redis, server_config=to_server_config(row),
                        )
                    )
                except Exception:  # noqa: BLE001 — bad row shouldn't stop others
                    log.warning("mcp.row.bad_config", extra={"id": str(row.id)})
        except Exception:  # noqa: BLE001 — never block the action on registry lookup
            log.warning("mcp.servers.lookup_failed", exc_info=True)

    fallback = _get_mcp_registry()
    if fallback is not None:
        out.append(fallback)
    return out


def _infer_provider_from_slug(slug: str) -> str:
    head = slug.split("_", 1)[0] if slug else ""
    return head.lower()

log = structlog.get_logger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


# ---------------------------------------------------------------------------
# Placeholder substitution
# ---------------------------------------------------------------------------


def _stem_variants(token: str) -> set[str]:
    """Generate normalized stems for fuzzy node-id matching."""
    base = (token or "").strip().lower()
    if not base:
        return set()
    variants = {base}
    head, sep, tail = base.partition("_")

    def _head_stems(word: str) -> set[str]:
        stems = {word}
        if word.endswith("ed") and len(word) > 3:
            stems.add(word[:-1])
            stems.add(word[:-2])
        for suffix in ("ing", "s"):
            if word.endswith(suffix) and len(word) > len(suffix) + 2:
                stems.add(word[: -len(suffix)])
        return stems

    if head:
        for hv in _head_stems(head):
            variants.add(f"{hv}{sep}{tail}" if tail else hv)
    return variants


def _fuzzy_output_key(key: str, outputs: dict[str, Any]) -> str | None:
    """Resolve common LLM typos in placeholder root keys (e.g. past tense)."""
    if key in outputs:
        return key
    key_stems = _stem_variants(key.split(".", 1)[0])
    matches: list[str] = []
    for candidate in outputs:
        cand_root = candidate.split(".", 1)[0]
        cand_stems = _stem_variants(cand_root)
        if key_stems & cand_stems:
            matches.append(candidate)
            continue
        if key.startswith(candidate) or candidate.startswith(key.split(".", 1)[0]):
            matches.append(candidate)
    if len(matches) == 1:
        return matches[0]
    return None


def _lookup(path: str, outputs: dict[str, Any]) -> Any:
    """Resolve ``upstream_id.a.b[0]`` against the outputs map.

    Outputs are the full ``node_id → value`` table the executor keeps;
    values may be strings, dicts, or lists. JSON-decode strings opportu-
    nistically so chained references work.
    """
    if not path:
        return None
    parts = path.replace("[", ".").replace("]", "").split(".")
    root = parts[0]
    resolved_root = _fuzzy_output_key(root, outputs) or root
    cur: Any = outputs.get(resolved_root)
    # Auto-JSON-decode if the upstream emitted a JSON string.
    if isinstance(cur, str):
        try:
            cur = json.loads(cur)
        except json.JSONDecodeError:
            pass
    # Agent nodes emit plain text — common placeholder suffixes map to it.
    if len(parts) > 1 and isinstance(outputs.get(resolved_root), str):
        alias = parts[1].lower()
        if alias in {"results", "content", "output", "text", "message", "response"}:
            return outputs.get(resolved_root)
    for p in parts[1:]:
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def render_placeholders(value: Any, outputs: dict[str, Any]) -> Any:
    """Walk a JSON-ish value and replace every ``{{ … }}`` reference.

    String placeholders that resolve to non-string values (dicts, lists)
    are preserved as-is *only* when the placeholder is the entire string;
    otherwise they're coerced via ``str(...)`` so they slot into the
    surrounding template. This matches what n8n does.
    """
    if isinstance(value, str):
        match = _PLACEHOLDER_RE.fullmatch(value.strip())
        if match:
            return _lookup(match.group(1).strip(), outputs)

        def _replace(m: re.Match[str]) -> str:
            v = _lookup(m.group(1).strip(), outputs)
            if v is None:
                return ""
            if isinstance(v, (dict, list)):
                return json.dumps(v, default=str)
            return str(v)

        return _PLACEHOLDER_RE.sub(_replace, value)
    if isinstance(value, list):
        return [render_placeholders(v, outputs) for v in value]
    if isinstance(value, dict):
        return {k: render_placeholders(v, outputs) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Action invocation
# ---------------------------------------------------------------------------


class ActionInvocationError(RuntimeError):
    """User-visible failure while invoking an action."""


# Slugs that represent a human-in-the-loop approval gate, not a real
# integration call.  When the LLM emits one of these as an ActionNode
# (instead of the proper human_handoff kind) we intercept it here so
# it doesn't reach Composio/MCP.
_HITL_SLUGS: frozenset[str] = frozenset(
    {
        "request_approval",
        "human_approval",
        "approval_gate",
        "hitl",
        "wait_for_approval",
        "approve",
        "approval",
    }
)

# Composio/HTTP providers that carry no real integration — the LLM
# sometimes uses these when it means "call an internal API".
_PSEUDO_PROVIDERS: frozenset[str] = frozenset({"http_bearer", "http_basic", "http", "https"})


_EMAIL_SLUG_FRAGMENTS: frozenset[str] = frozenset(
    {"send_email", "email", "send_mail", "compose_email"}
)


def _is_email_action(action_slug: str) -> bool:
    slug_lower = (action_slug or "").lower().replace("-", "_")
    return any(frag in slug_lower for frag in _EMAIL_SLUG_FRAGMENTS)


def _is_hitl_action(provider_id: str, action_slug: str) -> bool:
    slug_lower = (action_slug or "").lower().replace("-", "_")
    if slug_lower in _HITL_SLUGS:
        return True
    # http_bearer.request_approval pattern
    if (provider_id or "").lower() in _PSEUDO_PROVIDERS and slug_lower in _HITL_SLUGS:
        return True
    return False


# Verbs that classify an action as having a real-world side effect. Read-only
# verbs short-circuit to "safe". Anything else defaults to side-effecting so
# the publish-gate fails safe (an unknown action is treated as risky).
_READONLY_VERBS = (
    "read", "get", "list", "fetch", "search", "query", "lookup",
    "find", "describe", "count", "view", "load",
)
_WRITE_VERBS = (
    "send", "create", "write", "update", "delete", "post", "add",
    "insert", "remove", "upsert", "execute", "trigger", "reply",
    "forward", "publish", "move", "archive", "draft", "schedule",
)


def _is_side_effecting(provider_id: str, action_slug: str) -> bool:
    """Best-effort classification of whether an action changes the outside world.

    Local artifact generation (PDF) is handled before this and is never gated.
    Read-only verbs are safe; write verbs are side-effecting; unknown actions
    default to side-effecting (fail safe).
    """
    p = (provider_id or "").lower()
    s = (action_slug or "").lower().replace("-", "_")
    if p in {"pdf_generator", "pdf", "report"}:
        return False
    if any(v in s for v in _READONLY_VERBS):
        return False
    if any(v in s for v in _WRITE_VERBS):
        return True
    return True


async def invoke_action(
    *,
    provider_id: str,
    action_slug: str,
    params: dict[str, Any],
    workspace_connections: list[NativeConnection],
    allow_dry_run: bool = True,
    workspace_id: UUID | None = None,
    db: Any | None = None,
    live: bool = True,
    connection_id: str | None = None,
) -> dict[str, Any]:
    """Run one action; return a JSON-serialisable result dict.

    The result is always a dict so downstream nodes can JSONPath into it.
    Real invocations include ``__provider__``, ``__action__``, ``status``
    (when the underlying tool reported one), and ``data`` (the raw tool
    output). Dry-run stubs include ``__dry_run__: true`` plus a synthetic
    ``data`` payload mirroring the request — good enough for the visual
    editor to demonstrate flow without live credentials.

    ``live`` is the publish-gate. When ``False`` (the workflow is not
    published, or this is a test run), any *side-effecting* action — sending
    email, writing to a sheet/DB, posting to Slack — is intercepted and
    returned as a PREVIEW instead of being executed. Read-only actions and
    local artifacts (PDF) still run so the preview is realistic. This is the
    server-side guarantee that a draft can never touch the outside world.
    """
    prov = (provider_id or "").lower()
    slug = (action_slug or "").lower()
    if prov in {"pdf_generator", "pdf", "report"} or slug in {
        "create_pdf",
        "generate_pdf",
        "render_pdf",
    }:
        return _generate_pdf(params or {})

    # internal.sign_link (P7′): build a signed trigger link. Pure computation
    # (not side-effecting), so it runs in preview/draft too — the invite email
    # can show a real, tamper-proof link before publish.
    if prov == "internal" and slug == "sign_link":
        return _sign_link(params or {}, workspace_id=workspace_id)

    # internal.register_voice_route (P9): map a Retell call_id → the workflow to
    # fire when the call ends (the scoring workflow, referenced by trigger slug),
    # plus the candidate context. The Retell callback endpoint reads this back.
    if prov == "internal" and slug == "register_voice_route":
        return await _register_voice_route(params or {}, workspace_id=workspace_id, live=live)

    # internal.hr_pick_round / hr_advance: deterministic ladder helpers (pure
    # functions, no LLM, no side effects) — run in draft/preview too so the
    # round-aware recruitment chain has reliable structured control-flow.
    if prov == "internal" and slug == "llm_json":
        return await _llm_json(params or {})
    if prov == "internal" and slug == "hr_pick_round":
        return _hr_pick_round(params or {})
    if prov == "internal" and slug == "hr_advance":
        return _hr_advance(params or {})
    if prov == "internal" and slug == "hr_stack_rank":
        return _hr_stack_rank(params or {})
    if prov == "internal" and slug == "fire_workflow":
        return await _fire_workflow(params or {}, workspace_id=workspace_id)

    # ATS candidate search scaffold: in draft/demo (not live) return a sample
    # shortlist so the recruitment templates run end-to-end without a live ATS.
    # Live runs fall through to the real bearer-HTTP connector.
    if slug == "ats_search_candidates" and not live:
        return _ats_demo_stub(params or {})

    # Publish-gate: block real side effects unless the workflow is live.
    if not live and _is_side_effecting(prov, slug):
        log.info(
            "action_runner.publish_gate.preview",
            provider=provider_id,
            action=action_slug,
        )
        return {
            "__provider__": provider_id,
            "__action__": action_slug,
            "__dry_run__": True,
            "__preview__": True,
            "__blocked_reason__": "workflow_not_published",
            "data": {
                **(params or {}),
                "note": (
                    "PREVIEW — not executed because the workflow is not "
                    "published. This is exactly what would happen on a live "
                    "run. Publish the workflow to perform real actions."
                ),
            },
        }

    # ------------------------------------------------------------------
    # Guard: validate email recipient before calling Composio.
    # When the workflow template uses {{ input.email }} and the user didn't
    # supply it, all three recipient fields are empty strings — Composio
    # returns a 422 error which surfaces as a confusing crash.  Detect this
    # up-front and return a dry-run with a clear human-readable message.
    # ------------------------------------------------------------------
    if _is_email_action(action_slug):
        p = params or {}
        to_val = (p.get("to") or p.get("recipient_email") or "").strip()
        cc_val = (p.get("cc") or "").strip()
        bcc_val = (p.get("bcc") or "").strip()
        if not to_val and not cc_val and not bcc_val:
            log.info(
                "action_runner.email.missing_recipient",
                provider=provider_id,
                action=action_slug,
            )
            return {
                "__provider__": provider_id,
                "__action__": action_slug,
                "__dry_run__": True,
                "__reason__": "missing_recipient",
                "data": {
                    "ok": False,
                    "note": (
                        "Email not sent — no recipient address was provided. "
                        "Add a 'recipient_email' field to the workflow trigger form "
                        "or hard-code a 'to' address in the action params."
                    ),
                    "echo": p,
                },
            }

    # ------------------------------------------------------------------
    # Guard: intercept HITL slugs before they reach MCP/Composio.
    # The LLM sometimes emits an ActionNode with action_slug="request_approval"
    # instead of a proper human_handoff node kind.  Return a sentinel so the
    # executor can emit a hitl_required event and pause gracefully.
    # ------------------------------------------------------------------
    if _is_hitl_action(provider_id, action_slug):
        log.info(
            "action_runner.hitl_intercepted",
            provider=provider_id,
            action=action_slug,
        )
        return {
            "__provider__": provider_id,
            "__action__": action_slug,
            "__hitl_required__": True,
            "__dry_run__": False,
            "data": {
                "status": "pending_approval",
                "message": "Human approval required before proceeding.",
            },
        }

    # ------------------------------------------------------------------
    # Native Gmail send: when a native ``gmail`` connection is configured, send
    # via the Gmail REST API directly (MIME -> messages/send) rather than the
    # Composio meta-tool. Only on a live run — the publish gate above returns a
    # preview for drafts. Falls through to MCP/Composio when no native gmail
    # connection exists.
    # ------------------------------------------------------------------
    if live and slug == "gmail_send":
        gmail_conn = next(
            (
                c for c in workspace_connections
                if c.provider == "gmail"
                and c.status == NativeConnectionStatus.ACTIVE
            ),
            None,
        )
        if gmail_conn is not None:
            creds = decode_config(gmail_conn)
            # OAuth access tokens expire (~1h); refresh in-memory before sending
            # when a refresh_token is present. Best-effort — fall back to the
            # stored token if refresh isn't possible.
            try:
                from services.oauth2_service import (
                    get_oauth_provider,
                    refresh_token_if_needed,
                )

                oauth_prov = get_oauth_provider("gmail")
                if oauth_prov is not None:
                    refreshed = await refresh_token_if_needed(oauth_prov, creds)
                    if refreshed:
                        creds = refreshed
            except Exception:  # noqa: BLE001 — refresh is best-effort
                log.warning("action_runner.gmail.refresh_failed", exc_info=True)
            token = str(creds.get("access_token") or "").strip()
            if token:
                return await asyncio.to_thread(
                    _gmail_send_native, params or {}, access_token=token
                )

    # ------------------------------------------------------------------
    # Pre-flight: resolve any name-shaped param values (e.g.
    # ``spreadsheet_id: "ICICI Lombard Motor Renewal"``) into real IDs via
    # Composio Drive/Slack/Gmail lookups. Transparent to the action node
    # — the runtime substitutes IDs before either the MCP or native path.
    # ------------------------------------------------------------------
    registries = await _collect_mcp_registries(workspace_id=workspace_id, db=db)
    resolved_log: dict[str, dict[str, str]] = {}
    if provider_id and registries:
        from agents.resource_resolver import resolve_action_params

        params, resolved_log = await resolve_action_params(
            provider_id=provider_id,
            params=params,
            workspace_id=workspace_id,
            registries=registries,
        )

    # Prefer an explicitly-configured native connection over Composio/MCP.
    # Composio's fuzzy tool-matching can mis-route (e.g. ats_search_candidates ->
    # ASHBY_SEARCH_CANDIDATES) and requires a tool-router session we don't open,
    # so when the workspace has an active native connection for this provider we
    # skip Composio entirely and use the native path (Path B) below.
    native_prov = get_provider(provider_id) or resolve_provider_for_slug(action_slug)
    has_native_conn = native_prov is not None and any(
        c.provider == native_prov.id and c.status == NativeConnectionStatus.ACTIVE
        for c in workspace_connections
    )

    # ------------------------------------------------------------------
    # Path A — MCP/Composio (only when no native connection is configured).
    # Talks the MCP wire protocol directly, bypassing the broken legacy
    # ToolSet SDK shim. Tries each workspace-registered server first, then
    # falls back to the env-configured endpoint.
    # ------------------------------------------------------------------
    if not has_native_conn:
        mcp_result = await _try_invoke_via_mcp(
            provider_id=provider_id,
            action_slug=action_slug,
            params=params,
            registries=registries,
        )
        if mcp_result is not None:
            if resolved_log:
                mcp_result["__resolved_params__"] = resolved_log
            return mcp_result

    provider = get_provider(provider_id) or resolve_provider_for_slug(action_slug)
    if provider is None:
        if allow_dry_run:
            return _dry_run(provider_id, action_slug, params, reason="unknown_provider")
        raise ActionInvocationError(f"unknown provider: {provider_id!r}")

    # Find a workspace connection for this provider. If the node bound a
    # specific connection_id (multi-account), prefer that; otherwise use the
    # first active connection for the provider.
    active = [
        c for c in workspace_connections
        if c.provider == provider.id and c.status == NativeConnectionStatus.ACTIVE
    ]
    conn_row = None
    if connection_id:
        conn_row = next((c for c in active if str(c.id) == str(connection_id)), None)
    if conn_row is None:
        conn_row = active[0] if active else None
    if conn_row is None:
        if allow_dry_run:
            return _dry_run(
                provider.id, action_slug, params, reason="no_connection_configured"
            )
        raise ActionInvocationError(
            f"no active connection for provider {provider.id!r}"
        )

    if provider.build_connection is None or provider.build_tool is None:
        if allow_dry_run:
            return _dry_run(
                provider.id, action_slug, params, reason="provider_not_invokable"
            )
        raise ActionInvocationError(
            f"provider {provider.id!r} cannot be invoked directly"
        )

    cfg = decode_config(conn_row)
    try:
        conn = provider.build_connection(cfg)
        tool = provider.build_tool(conn, action_slug)
    except Exception as exc:  # noqa: BLE001
        if allow_dry_run:
            return _dry_run(
                provider.id, action_slug, params,
                reason=f"build_failed: {exc}",
            )
        raise ActionInvocationError(f"build failed: {exc}") from exc

    if tool is None:
        if allow_dry_run:
            return _dry_run(
                provider.id, action_slug, params,
                reason="no_tool_builder",
            )
        raise ActionInvocationError(
            f"provider {provider.id!r} has no tool builder for slug {action_slug!r}"
        )

    # Execute the tool. Dynamiq tool nodes implement a sync ``execute(input_data)``
    # that returns the response payload; offload to a thread so we don't block.
    try:
        result = await asyncio.to_thread(_run_tool, tool, params)
    except Exception as exc:  # noqa: BLE001
        if allow_dry_run:
            return _dry_run(
                provider.id, action_slug, params,
                reason=f"invocation_failed: {exc}",
            )
        raise ActionInvocationError(str(exc)) from exc

    # Dynamiq HTTP nodes return an envelope {"content": <body>, "status_code": N}.
    # Surface the API body as the node's `data` — so `$.data` matches both the
    # connector contract and the demo-stub shape — and treat >=400 as a failure
    # so on_error routing still fires.
    if isinstance(result, dict) and "status_code" in result and "content" in result:
        status_code = result.get("status_code")
        if isinstance(status_code, int) and status_code >= 400:
            if allow_dry_run:
                return _dry_run(
                    provider.id, action_slug, params, reason=f"http_{status_code}"
                )
            raise ActionInvocationError(
                f"{provider.id}.{action_slug} HTTP {status_code}: "
                f"{str(result.get('content'))[:200]}"
            )
        result = result["content"]

    out_native: dict[str, Any] = {
        "__provider__": provider.id,
        "__action__": action_slug,
        "__dry_run__": False,
        "data": _safe_jsonable(result),
    }
    if resolved_log:
        out_native["__resolved_params__"] = resolved_log
    return out_native


def _run_tool(tool: Any, params: dict[str, Any]) -> Any:
    """Invoke a Dynamiq tool node uniformly.

    Dynamiq nodes (``HttpApiCall``, ``SQLExecutor``, …) validate their input
    schema in ``.run(input_data=...)`` and only then call ``.execute`` with a
    validated model — so we must call ``.run``, never ``.execute`` directly
    (``.execute`` assumes an already-parsed model and blows up on a raw dict).
    ``.run`` returns a ``RunnableResult``; surface its ``.output`` and treat a
    non-SUCCESS status as a failure so ``on_error`` handling kicks in.
    """
    if hasattr(tool, "run"):
        res = tool.run(input_data=params)
        status = getattr(res, "status", None)
        if status is not None and getattr(status, "name", str(status)).upper() != "SUCCESS":
            raise RuntimeError(
                f"tool {getattr(tool, 'name', '?')} failed: "
                f"{getattr(res, 'error', None) or getattr(res, 'output', None)}"
            )
        return getattr(res, "output", res)
    # Fall back to a direct ``__call__`` for hand-rolled tools.
    return tool(params)


def _safe_jsonable(val: Any) -> Any:
    try:
        json.dumps(val)
        return val
    except (TypeError, ValueError):
        if hasattr(val, "model_dump"):
            try:
                return val.model_dump(mode="json")
            except Exception:  # noqa: BLE001
                pass
        return str(val)


def _dry_run(
    provider_id: str,
    action_slug: str,
    params: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    log.info(
        "action_runner.dry_run",
        provider=provider_id,
        action=action_slug,
        reason=reason,
    )
    note = (
        f"dry-run: connect a {provider_id!r} integration to make this "
        "node actually call the upstream API"
    )
    data: dict[str, Any] = {
        "ok": True,
        "echo": params,
        "note": note,
    }
    try:
        from services.mock_responses import mock_for_action  # noqa: PLC0415

        realistic = mock_for_action(provider_id, action_slug, params)
    except Exception:  # noqa: BLE001
        realistic = None
    if realistic:
        merged: dict[str, Any] = {}
        merged.update(realistic)
        merged.update(data)
        data = merged
    return {
        "__provider__": provider_id,
        "__action__": action_slug,
        "__dry_run__": True,
        "__reason__": reason,
        "data": data,
    }


__all__ = [
    "ActionInvocationError",
    "invoke_action",
    "render_placeholders",
]
