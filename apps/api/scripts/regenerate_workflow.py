"""Regenerate a workflow's latest version via the improved interpreter.

Usage (run inside the api container):

    python -m scripts.regenerate_workflow <workflow_id> [--prompt-file PATH]

The script:
    1. Loads the existing workflow row + latest definition (for the workspace
       id and a description fallback).
    2. Builds the tools list the same way ``interpret_and_preview`` does
       (native catalog + Composio MCP names + preview defaults).
    3. Calls ``WorkflowInterpreter.interpret`` with a rich prompt that
       names the real sheet + columns, so the LLM emits operational
       agent instructions and bounded ranges.
    4. Inserts a new ``workflow_versions`` row and bumps
       ``workflows.current_version`` so the next ``/run`` picks it up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from core.config import get_settings
from core.database import get_session_factory
from core.redis import get_redis
from egpt_mcp.mcp_tool_registry import MCPToolError, MCPToolRegistry
from egpt_mcp.tool_registry import ToolRegistry
from models.workflow import Workflow as WFRow
from models.workflow_version import WorkflowVersion
from schemas.workflow import WorkflowDefinition
from services.workflow_interpreter import WorkflowInterpreter

DEFAULT_PROMPT = """\
Build an end-to-end Customer Lifecycle Management AI Agent for ICICI
Lombard motor-insurance renewals.

Data source: Google Sheets spreadsheet
``1eQS42wdsyryLvkKEysFFKJR54_qsC-sBzSSi6TbfupY`` (display name
"ICICI_Lombard_Motor_Renewal_SimData"). The relevant worksheet is
``Customer_Master`` with the following columns:
Customer_ID, Full_Name, Gender, Age, Email, Mobile, City, Pincode,
State, Occupation, Annual_Income_Band, Preferred_Language,
Customer_Since, Opt_In_SMS, Opt_In_Email, Opt_In_WhatsApp,
Opt_In_Call, KYC_Verified. There are ~1500 rows.

Pipeline (use BOUNDED ranges like ``Customer_Master!A1:R500`` so the
Composio MCP returns inline data):

1. Manual trigger.
2. Fetch the first 500 customer rows from Customer_Master using the
   Google Sheets read action (provider=googlesheets, action_slug=
   read_range, range="Customer_Master!A1:R500", spreadsheetId=
   "1eQS42wdsyryLvkKEysFFKJR54_qsC-sBzSSi6TbfupY").
3. Categorise customers (LLM agent with COMPOSIO_SEARCH_TOOLS +
   COMPOSIO_MULTI_EXECUTE_TOOL). Instructions MUST: name the upstream
   ``fetch_customer_data`` as source-of-truth; analyse the rows
   already in the input; if a partial preview marker is detected,
   re-fetch via GOOGLESHEETS_VALUES_GET with a bounded range; output
   4-6 NAMED segments using demographics / geography / tenure /
   opt-in flags; for EACH segment list size, criteria, 2-3 verbatim
   example customers (Customer_ID + Full_Name), recommended
   engagement tactic; finish with an ``engagement_targets`` JSON
   array of the top-30 customers (Customer_ID, Full_Name, Email,
   segment) — emails MUST come from the data, no invention; opt-in
   must be honoured (Opt_In_Email == "Yes" only).
4. ForEach over ``engagement_targets`` from step 3. The for_each
   ``body`` MUST be a string array of node ids (not inline node
   objects). Define a SEPARATE top-level ``action`` node with id
   ``send_renewal_nudge`` whose ``depends_on`` is ``["nudge_loop"]``
   and reference it as ``"body": ["send_renewal_nudge"]``. The
   action sends a personalised renewal nudge email via Composio
   Gmail (COMPOSIO_MULTI_EXECUTE_TOOL with tool_slug=GMAIL_SEND_EMAIL).
   Use ``to: {{ candidate.Email }}``, ``subject``, and a body that
   references ``{{ candidate.Full_Name }}``. NO empty recipients —
   the upstream categoriser already filtered them.
5. Human approval gate (wait_for_webhook or human handoff) — pauses
   for ops to sign off the campaign before the loop runs (place it
   before the for_each).
6. Gather customer feedback (action: Composio Gmail
   GMAIL_FETCH_EMAILS, search="newer_than:7d label:Inbox reply"). If
   none, return empty list (do NOT pivot to Gmail contacts).
7. Analyse feedback sentiment (LLM agent). Instructions MUST: name
   ``gather_feedback`` as source-of-truth; if no real feedback text
   is present, return ``{"status":"no_feedback_available",
   "reason":"..."}``; otherwise per-message sentiment + aggregate
   summary with counts and top themes; NO fabricated quotes.
8. Generate analysis report (action provider=pdf_generator,
   action_slug=create_pdf). content placeholder must reference both
   categorize_customers and analyze_feedback outputs verbatim — use
   ``{{ categorize_customers }}\\n\\n{{ analyze_feedback }}`` so the
   agent's full markdown is rendered.

Output format: markdown.
"""


async def _gather_tools(ws_id: UUID) -> list[str]:
    settings = get_settings()
    redis = get_redis()
    names: set[str] = set(settings.workflow_preview_tools)

    factory = get_session_factory()
    async with factory() as db:
        registry = ToolRegistry(settings, redis)
        try:
            registry_names = await registry.get_tool_names_for_prompt(db, ws_id)
            names.update(registry_names)
        except Exception:  # noqa: BLE001
            pass

    try:
        mcp = MCPToolRegistry(settings, redis)
        if mcp._is_enabled():
            tools = await mcp.list_tools()
            for t in tools:
                n = str(t.get("name") or "")
                if n:
                    names.add(n)
    except MCPToolError:
        pass
    except Exception:  # noqa: BLE001
        pass

    return sorted(names)


async def regenerate(workflow_id: UUID, prompt: str) -> None:
    settings = get_settings()
    interp = WorkflowInterpreter(settings)

    factory = get_session_factory()
    async with factory() as db:
        row = (
            await db.execute(select(WFRow).where(WFRow.id == workflow_id))
        ).scalar_one_or_none()
        if row is None:
            raise SystemExit(f"workflow {workflow_id} not found")
        ws_id: UUID = row.workspace_id

        tools = await _gather_tools(ws_id)
        print(f"[regen] workspace={ws_id} tools_available={len(tools)}")

        wd: WorkflowDefinition = await interp.interpret(
            user_input=prompt, available_tools=tools
        )
        print(f"[regen] interpreted node_count={len(wd.nodes)} name={wd.name!r}")

        row.current_version = (row.current_version or 0) + 1
        ver = WorkflowVersion(
            workflow_id=row.id,
            version=row.current_version,
            definition=wd.model_dump(mode="json"),
            change_note="regenerated via improved interpreter",
            created_by=row.created_by,
        )
        db.add(ver)
        if wd.name and wd.name.strip():
            row.name = wd.name.strip()
        await db.commit()
        print(
            f"[regen] persisted workflow_id={row.id} new_version={row.current_version}"
        )
        print(json.dumps(wd.model_dump(mode="json")["nodes"], indent=2)[:2000])


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("workflow_id", type=UUID)
    p.add_argument(
        "--prompt-file", type=Path, default=None,
        help="path to a UTF-8 text file containing the user prompt",
    )
    args = p.parse_args()
    prompt = (
        args.prompt_file.read_text(encoding="utf-8") if args.prompt_file else DEFAULT_PROMPT
    )
    asyncio.run(regenerate(args.workflow_id, prompt))


if __name__ == "__main__":
    sys.exit(main())
