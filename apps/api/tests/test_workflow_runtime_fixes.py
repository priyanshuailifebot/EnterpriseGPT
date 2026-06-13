"""Regression tests for generic workflow runtime fixes."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from agents.action_runner import _bound_sheet_range, _lookup, render_placeholders
from agents.dynamiq_service import DynamiqService
from agents.extended_executor import (
    _evaluate_if,
    _extract_upstream_text,
    _role_specific_rules,
    _summarise_composio_preview,
)
from agents.resource_resolver import _pick_best_data_tab
from core.config import Settings
from schemas.workflow import AgentNode, WorkflowDefinition


def test_if_unresolved_jsonpath_is_not_truthy() -> None:
    """``$.missing != null`` must be false when the field does not exist."""
    outputs = {"send_nudges": {"ok": False}}
    assert _evaluate_if("$.customer_response != null", outputs) == "false"


def test_if_null_comparison_when_field_missing() -> None:
    outputs: dict = {}
    assert _evaluate_if("$.foo == null", outputs) == "true"


def test_placeholder_fuzzy_node_id_stem_match() -> None:
    outputs = {
        "categorize_customers": {
            "email": "user@example.com",
            "name": "Ada",
        }
    }
    rendered = render_placeholders(
        {"to": "{{ categorized_customers.email }}"},
        outputs,
    )
    assert rendered["to"] == "user@example.com"


def test_agent_tool_bindings_reads_v2_nodes() -> None:
    wd = WorkflowDefinition(
        name="demo",
        nodes=[
            AgentNode(
                id="agent_a",
                name="A",
                tools=["COMPOSIO_SEARCH_TOOLS", "native_tool"],
            )
        ],
    )
    bindings = wd.agent_tool_bindings()
    assert bindings == {"agent_a": ["COMPOSIO_SEARCH_TOOLS", "native_tool"]}


def test_extract_upstream_text_flattens_sheet_values() -> None:
    payload = {
        "data": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"data":{"results":[{"response":{"data":'
                        '{"valueRanges":[{"values":[["Name","Email"],'
                        '["Ada","ada@test.com"]]}]}}}]}}'
                    ),
                }
            ]
        }
    }
    text = _extract_upstream_text(payload)
    assert "Ada,ada@test.com" in text


def test_lookup_agent_text_results_alias() -> None:
    outputs = {
        "analyze_feedback": (
            "Sentiment summary: 12 positive, 3 neutral, 1 negative."
        )
    }
    assert _lookup("analyze_feedback.results", outputs) == outputs["analyze_feedback"]
    rendered = render_placeholders(
        {"content": "{{ analyze_feedback.results }}"},
        outputs,
    )
    assert "Sentiment summary" in rendered["content"]


def test_pick_best_data_tab_prefers_customer_master() -> None:
    tabs = ["README", "Sheet1", "Customer_Master", "Notes"]
    assert _pick_best_data_tab(tabs) == "Customer_Master"


def test_build_agent_mcp_meta_tools_wires_composio_slugs() -> None:
    wd = WorkflowDefinition(
        name="demo",
        nodes=[
            AgentNode(
                id="agent_a",
                name="A",
                tools=["COMPOSIO_SEARCH_TOOLS", "COMPOSIO_MULTI_EXECUTE_TOOL"],
            )
        ],
    )
    registry = MagicMock()
    registry.call_tool = AsyncMock(return_value={"successful": True, "data": {}})
    svc = DynamiqService(Settings())
    mapping = svc.build_agent_mcp_meta_tools(
        wd,
        registry=registry,
        execution_id=None,
        tool_run_buffer=None,
    )
    assert "agent_a" in mapping
    names = {getattr(t, "name", None) for t in mapping["agent_a"]}
    assert names == {"COMPOSIO_SEARCH_TOOLS", "COMPOSIO_MULTI_EXECUTE_TOOL"}


def test_bound_sheet_range_caps_open_ended() -> None:
    assert _bound_sheet_range("Customer_Master!A:R") == "Customer_Master!A1:R200"
    assert _bound_sheet_range("A:Z") == "A1:Z200"
    # Already bounded → untouched.
    assert _bound_sheet_range("Sheet1!A1:R150") == "Sheet1!A1:R150"
    # Single-cell or invalid → untouched.
    assert _bound_sheet_range("A1") == "A1"


def test_result_is_preview_only_detects_data_preview_envelope() -> None:
    from agents.action_runner import _result_is_preview_only

    preview = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({
                    "data": {
                        "results": [
                            {"response": {"data_preview": {"values": [["x"]]}}}
                        ]
                    }
                }),
            }
        ]
    }
    full = {
        "content": [
            {
                "type": "text",
                "text": json.dumps({
                    "data": {"valueRanges": [{"values": [["a", "b"]]}]}
                }),
            }
        ]
    }
    assert _result_is_preview_only(preview) is True
    assert _result_is_preview_only(full) is False


def test_halve_sheet_range_halves_until_floor() -> None:
    from agents.action_runner import _halve_sheet_range

    assert _halve_sheet_range("Customer_Master!A1:R200") == (
        "Customer_Master!A1:R100",
        100,
    )
    assert _halve_sheet_range("A1:Z80") == ("A1:Z40", 40)
    assert _halve_sheet_range("Customer_Master!A1:R50") == (
        "Customer_Master!A1:R25",
        25,
    )
    new_range, new_max = _halve_sheet_range("A:Z")
    assert new_max == 0  # Unbounded → no-op.


def test_summarise_composio_preview_emits_partial_marker() -> None:
    payload = {
        "data": {
            "results": [
                {
                    "response": {
                        "data_preview": {
                            "range": "Customer_Master!A1:C1501",
                            "values": [
                                ["Customer_ID", "...2 more items"],
                                ["CUST100001", "...2 more items"],
                                "...1499 more items",
                            ],
                        }
                    },
                    "tool_slug": "GOOGLESHEETS_VALUES_GET",
                }
            ],
            "remote_file_info": {"file_path": "/mnt/files/mex/food.json"},
        }
    }
    text = _summarise_composio_preview(payload)
    assert "PARTIAL PREVIEW ONLY" in text
    assert "GOOGLESHEETS_VALUES_GET" in text
    assert "Customer_Master" in text


def test_extract_upstream_text_uses_partial_preview_when_no_inline_values() -> None:
    payload = {
        "data": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        '{"data":{"results":[{"response":{"successful":true,'
                        '"data_preview":{"range":"Customer_Master!A1:C1501",'
                        '"values":[["Customer_ID","...2 more items"],'
                        '["CUST100001","...2 more items"],'
                        '"...1499 more items"]}},'
                        '"tool_slug":"GOOGLESHEETS_VALUES_GET"}]}}'
                    ),
                }
            ]
        }
    }
    text = _extract_upstream_text(payload)
    assert "PARTIAL PREVIEW ONLY" in text
    assert "GOOGLESHEETS_VALUES_GET" in text


def test_role_specific_rules_for_categorisation() -> None:
    class _N:
        role = "Customer Categorization"
        name = "Categorize Customers"
        id = "categorize_customers"

    rules = _role_specific_rules(_N(), {"fetch_customer_data": "rows..."})
    assert "Customer segmentation analyst" in rules
    assert "engagement_targets" in rules
    assert "fetch_customer_data" in rules


def test_role_specific_rules_for_sentiment_no_data() -> None:
    class _N:
        role = "Sentiment Analysis"
        name = "Analyze Customer Feedback"
        id = "analyze_feedback"

    rules = _role_specific_rules(_N(), {"gather_feedback": "..."})
    assert "Sentiment / feedback analyst" in rules
    assert "no_feedback_available" in rules
    assert "fabricate" in rules
