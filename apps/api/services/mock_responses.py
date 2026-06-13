"""Realistic mock payloads for dry-run / demo executions.

When the workflow runner can't actually call an upstream API (no connection
configured, or we're in the visual-canvas "Test workflow" demo path), we
still want the response payload to look like the real provider's response
shape. That way downstream agents (LLM classifiers, output parsers, etc.)
receive data that resembles what they'll see in production and can be
exercised end-to-end.

Two entry points:

* :func:`mock_for_action` — keyed by Composio provider id + action slug.
  Returns ``None`` when we don't have a known shape for that combination,
  so callers can fall back to a bare echo envelope.
* :func:`mock_for_data_store` — keyed by operation (``read`` / ``write`` /
  ``query``). Always returns a dict.

Both ``demo_executor`` and ``action_runner._dry_run`` import from here so
that dry-run / demo behaviour stays consistent.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

__all__ = ["mock_for_action", "mock_for_data_store"]


def _short_code(prefix: str, slug: str, params: dict[str, Any], digits: int = 6) -> str:
    """Deterministic, real-looking id (e.g. ``TICKET-482913``).

    Derived from the slug + params so a given call always yields the same id —
    no randomness/clock (demo runs must stay deterministic), but it looks like
    a real record id in the test-run output.
    """
    basis = f"{slug}|{json.dumps(params, sort_keys=True, default=str)}"
    n = int(hashlib.sha1(basis.encode()).hexdigest(), 16) % (10 ** digits)
    return f"{prefix}-{n:0{digits}d}"


def _mock_business_action(slug_raw: str, params: dict[str, Any]) -> dict[str, Any] | None:
    """Realistic payloads for common *internal* business actions (CRM,
    ticketing, scheduling, dashboards) regardless of provider id.

    Makes the demo look production-real — a created ticket shows a ticket id, a
    registration shows a customer id, an escalation shows the assigned team —
    instead of a bare echo stub.
    """
    s = (slug_raw or "").strip().lower()

    def has(*frags: str) -> bool:
        return any(f in s for f in frags)

    if has("escalate"):
        return {
            "escalation_id": _short_code("ESC", s, params, 5),
            "status": "escalated",
            "assigned_team": "Tier-2 Support",
            "priority": "high",
            "sla_hours": 4,
            "summary": "Complaint escalated to Tier-2 Support (SLA 4h).",
        }
    if has("register", "create_customer", "create_crm", "add_customer"):
        cid = _short_code("CUST", s, params)
        return {
            "customer_id": cid,
            "status": "active",
            "created": True,
            "summary": f"New customer {cid} registered in CRM.",
        }
    if has("ticket"):  # create_ticket / raise_ticket / open_ticket / update_ticket
        tid = _short_code("TICKET", s, params)
        resolved = has("resolve", "close")
        return {
            "ticket_id": tid,
            "status": "resolved" if resolved else "open",
            "priority": "medium",
            "summary": f"Ticket {tid} {'resolved' if resolved else 'created'}.",
        }
    if has("schedule", "book", "calendar"):
        eid = _short_code("EVT", s, params, 5)
        return {
            "event_id": eid,
            "status": "scheduled",
            "summary": f"Interview/meeting {eid} scheduled.",
        }
    if has("dashboard", "update_status", "set_status"):
        return {
            "record_id": _short_code("DASH", s, params, 5),
            "status": "updated",
            "summary": "Dashboard record updated with latest status.",
        }
    if has("respond", "reply", "send_response", "send_message", "notify"):
        return {
            "message_id": _short_code("MSG", s, params),
            "status": "queued",
            "channel": params.get("channel") or "email",
            "summary": "Response composed for the customer.",
        }
    return None


_SAMPLE_SHEET_ROWS: list[list[str]] = [
    ["id", "name", "email", "status", "amount", "created_at"],
    ["1", "Acme Corp", "ops@acme.example", "active", "1250.00", "2026-05-01T09:14:00Z"],
    ["2", "Globex Inc", "hello@globex.example", "pending", "480.50", "2026-05-03T16:42:11Z"],
    ["3", "Initech", "billing@initech.example", "active", "3200.00", "2026-05-08T11:05:33Z"],
    ["4", "Umbrella LLC", "contact@umbrella.example", "churned", "0.00", "2026-05-12T08:00:00Z"],
    ["5", "Stark Industries", "sales@stark.example", "active", "9875.25", "2026-05-19T13:27:48Z"],
]


def _normalize_slug(slug: str) -> str:
    s = (slug or "").strip().upper()
    for prefix in ("GOOGLESHEETS_", "GMAIL_", "GOOGLEDRIVE_", "SENDGRID_"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def _normalize_provider(provider_id: str) -> str:
    return (provider_id or "").strip().lower().replace("-", "").replace("_", "")


def _mock_googlesheets(slug: str, params: dict[str, Any]) -> dict[str, Any] | None:
    rng = (
        params.get("range")
        or params.get("a1Range")
        or params.get("a1_range")
        or "Sheet1!A1:F6"
    )
    spreadsheet_id = (
        params.get("spreadsheet_id")
        or params.get("spreadsheetId")
        or "demo-spreadsheet-id"
    )

    if slug in {"VALUES_GET", "GET_VALUES", "READ_RANGE", "READ"}:
        return {
            "range": rng,
            "majorDimension": "ROWS",
            "values": [list(row) for row in _SAMPLE_SHEET_ROWS],
            "spreadsheetId": spreadsheet_id,
        }

    if slug in {"BATCH_GET", "VALUES_BATCH_GET", "BATCH_GET_VALUES"}:
        ranges = params.get("ranges") or [rng]
        if isinstance(ranges, str):
            ranges = [ranges]
        return {
            "spreadsheetId": spreadsheet_id,
            "valueRanges": [
                {
                    "range": r,
                    "majorDimension": "ROWS",
                    "values": [list(row) for row in _SAMPLE_SHEET_ROWS],
                }
                for r in ranges
            ],
        }

    if slug in {"VALUES_UPDATE", "UPDATE_VALUES", "WRITE_RANGE", "UPDATE"}:
        values = params.get("values") or params.get("valueInputOption") or []
        updated_rows = len(values) if isinstance(values, list) else 1
        updated_cells = sum(
            len(r) if isinstance(r, list) else 1
            for r in (values if isinstance(values, list) else [values])
        ) or 1
        return {
            "spreadsheetId": spreadsheet_id,
            "updatedRange": rng,
            "updatedRows": updated_rows,
            "updatedColumns": max(
                (len(r) for r in values if isinstance(r, list)), default=1
            ),
            "updatedCells": updated_cells,
        }

    return None


def _mock_gmail(slug: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if slug in {"SEND_EMAIL", "SEND", "SEND_MAIL"}:
        return {
            "message_id": "demo-msg-000000000001",
            "thread_id": "demo-thread-000000000001",
            "labels": ["SENT"],
            "to": params.get("to") or params.get("recipient") or "recipient@example.com",
            "subject": params.get("subject") or "(no subject)",
            "status": "queued",
        }

    if slug in {"FETCH_EMAILS", "LIST_EMAILS", "LIST", "FETCH"}:
        query = params.get("query") or params.get("q") or ""
        max_results = int(params.get("max_results") or params.get("maxResults") or 5)
        messages = [
            {
                "id": f"demo-msg-{i:012d}",
                "threadId": f"demo-thread-{i:012d}",
                "snippet": f"This is a demo email snippet #{i} matching {query!r}.",
                "from": f"sender{i}@example.com",
                "to": "you@example.com",
                "subject": f"Demo subject {i}",
                "internalDate": "1716000000000",
                "labelIds": ["INBOX", "UNREAD"],
            }
            for i in range(1, max_results + 1)
        ]
        return {
            "messages": messages,
            "resultSizeEstimate": len(messages),
            "nextPageToken": None,
        }

    return None


def _mock_googledrive(slug: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if slug in {"FIND_FILE", "FIND", "SEARCH_FILES"}:
        name = (
            params.get("name")
            or params.get("query")
            or params.get("q")
            or "demo-file.csv"
        )
        return {
            "files": [
                {
                    "id": "demo-file-id-0001",
                    "name": str(name),
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "webViewLink": "https://drive.google.com/file/d/demo-file-id-0001/view",
                    "modifiedTime": "2026-05-20T10:00:00Z",
                    "size": "4096",
                }
            ],
            "nextPageToken": None,
        }

    return None


def _mock_sendgrid(slug: str, params: dict[str, Any]) -> dict[str, Any] | None:
    if slug in {"SEND_EMAIL", "SEND", "MAIL_SEND"}:
        return {
            "message_id": "demo-sg-000000000001",
            "status_code": 202,
            "to": params.get("to") or params.get("recipient") or "recipient@example.com",
            "subject": params.get("subject") or "(no subject)",
            "accepted": True,
        }

    return None


_PROVIDER_DISPATCH = {
    "googlesheets": _mock_googlesheets,
    "gmail": _mock_gmail,
    "googledrive": _mock_googledrive,
    "sendgrid": _mock_sendgrid,
}


def mock_for_action(
    provider_id: str,
    action_slug: str,
    params: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return a realistic mock payload for ``provider_id.action_slug``.

    The returned dict is intended to be merged into a dry-run envelope by
    the caller. When we don't have a known shape for the requested combo,
    returns ``None`` so the caller can fall back to a bare echo.
    """
    provider = _normalize_provider(provider_id)
    builder = _PROVIDER_DISPATCH.get(provider)
    if builder is not None:
        result = builder(_normalize_slug(action_slug), params or {})
        if result is not None:
            return result
    # Fall back to a generic business-action mock keyed on the slug — covers
    # internal providers (http_bearer, postgres, custom) for ticketing/CRM/etc.
    return _mock_business_action(action_slug, params or {})


def mock_for_data_store(
    operation: str,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a realistic mock payload for a data-store op."""
    op = (operation or "").strip().lower()
    p = params or {}
    table = p.get("table")
    key = p.get("key")

    if op == "read":
        return {
            "table": table,
            "key": key,
            "row": {
                "id": "demo-1",
                "name": "Sample Row",
                "created_at": "2026-05-18T00:00:00Z",
            },
        }
    if op == "query":
        return {
            "table": table,
            "filter": p.get("filter") or {},
            "rows": [
                {"id": f"demo-{i}", "name": f"Sample Row {i}"} for i in range(1, 3)
            ],
        }
    return {
        "table": table,
        "key": key,
        "wrote": p.get("payload") or {},
    }
