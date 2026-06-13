"""Voice-Interview MCP server (SSE transport).

Exposes a small Model Context Protocol surface that EnterpriseGPT (or any
MCP-capable client) can plug into via the existing ``mcp`` native provider.
Four tools are advertised:

* ``start_interview`` — places an outbound call via Retell and returns the
  vendor's ``call_id``.
* ``get_interview_status`` — polls Retell for a call's lifecycle state.
* ``get_interview_transcript`` — returns the latest transcript.
* ``score_interview`` — runs the transcript through a zero-temperature LLM
  scoring prompt covering 8 rubric dimensions.

Retell is the default vendor because its REST API maps cleanly to a "phone
+ system prompt" call. Switch to Vapi by setting ``VOICE_VENDOR=vapi`` —
both vendors expose comparable shapes; the differences are isolated in the
``_vendor_*`` helpers below.

Run it:

    pip install -r requirements.txt
    export RETELL_API_KEY=...
    export VOICE_MCP_BEARER=...          # token clients must present
    uvicorn server:app --host 0.0.0.0 --port 8090
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse


VENDOR = os.getenv("VOICE_VENDOR", "retell").strip().lower()
RETELL_API_KEY = os.getenv("RETELL_API_KEY", "").strip()
VAPI_API_KEY = os.getenv("VAPI_API_KEY", "").strip()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
SCORING_MODEL = os.getenv("SCORING_MODEL", "gpt-4o-mini")
BEARER = os.getenv("VOICE_MCP_BEARER", "").strip()


# ---------------------------------------------------------------------------
# Tool catalogue advertised over MCP
# ---------------------------------------------------------------------------


_TOOLS: list[dict[str, Any]] = [
    {
        "name": "start_interview",
        "description": (
            "Place an outbound conversational interview call. The vendor "
            "handles telephony, STT, LLM turn-taking, TTS, and barge-in. "
            "Pass the candidate phone (E.164), JD summary, preferred "
            "language code, and a rubric — the call runs autonomously and "
            "this tool returns immediately with a call_id."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["candidate_phone", "jd_summary"],
            "properties": {
                "candidate_phone": {"type": "string", "description": "E.164 phone"},
                "jd_summary": {"type": "string"},
                "language": {
                    "type": "string",
                    "default": "en-US",
                    "description": "BCP-47 language tag",
                },
                "rubric": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Dimensions to score, e.g. ['local_market','communication']",
                },
                "candidate_name": {"type": "string"},
            },
        },
    },
    {
        "name": "get_interview_status",
        "description": "Return the current call state (ringing/in_progress/ended/failed).",
        "inputSchema": {
            "type": "object",
            "required": ["call_id"],
            "properties": {"call_id": {"type": "string"}},
        },
    },
    {
        "name": "get_interview_transcript",
        "description": "Return the call's transcript as a list of {role, text} turns.",
        "inputSchema": {
            "type": "object",
            "required": ["call_id"],
            "properties": {"call_id": {"type": "string"}},
        },
    },
    {
        "name": "score_interview",
        "description": (
            "Score a completed interview against an 8-dimension rubric using a "
            "zero-temperature LLM call. Returns per-dimension 0-100 scores plus "
            "an overall percentage."
        ),
        "inputSchema": {
            "type": "object",
            "required": ["call_id"],
            "properties": {
                "call_id": {"type": "string"},
                "rubric": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
    },
]


_DEFAULT_RUBRIC = (
    "local_market_knowledge",
    "relevant_industry_experience",
    "communication_skills",
    "past_experience",
    "customer_engagement_approach",
    "consultative_approach",
    "objection_handling",
    "customer_advisor_skills",
)


# ---------------------------------------------------------------------------
# Vendor adapters
# ---------------------------------------------------------------------------


async def _vendor_start_call(args: dict[str, Any]) -> dict[str, Any]:
    """Vendor-specific outbound call creation. Returns ``{call_id, vendor}``."""
    if VENDOR == "retell":
        if not RETELL_API_KEY:
            raise RuntimeError("RETELL_API_KEY not configured")
        agent_id = os.getenv("RETELL_AGENT_ID", "").strip()
        from_number = os.getenv("RETELL_FROM_NUMBER", "").strip()
        if not agent_id or not from_number:
            raise RuntimeError(
                "RETELL_AGENT_ID and RETELL_FROM_NUMBER must be configured "
                "(Retell agent owns the system prompt + voice; we just "
                "provide dynamic variables here)."
            )
        body = {
            "from_number": from_number,
            "to_number": args["candidate_phone"],
            "override_agent_id": agent_id,
            "retell_llm_dynamic_variables": {
                "jd_summary": args.get("jd_summary", ""),
                "candidate_name": args.get("candidate_name", "the candidate"),
                "language": args.get("language", "en-US"),
                "rubric": ", ".join(args.get("rubric") or _DEFAULT_RUBRIC),
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.retellai.com/v2/create-phone-call",
                json=body,
                headers={
                    "Authorization": f"Bearer {RETELL_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"retell rejected call: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
        return {"call_id": data.get("call_id"), "vendor": "retell"}

    if VENDOR == "vapi":
        if not VAPI_API_KEY:
            raise RuntimeError("VAPI_API_KEY not configured")
        assistant_id = os.getenv("VAPI_ASSISTANT_ID", "").strip()
        phone_id = os.getenv("VAPI_PHONE_NUMBER_ID", "").strip()
        if not assistant_id or not phone_id:
            raise RuntimeError("VAPI_ASSISTANT_ID and VAPI_PHONE_NUMBER_ID must be configured")
        body = {
            "assistantId": assistant_id,
            "phoneNumberId": phone_id,
            "customer": {"number": args["candidate_phone"]},
            "assistantOverrides": {
                "variableValues": {
                    "jd_summary": args.get("jd_summary", ""),
                    "candidate_name": args.get("candidate_name", "the candidate"),
                    "language": args.get("language", "en-US"),
                }
            },
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.vapi.ai/call",
                json=body,
                headers={
                    "Authorization": f"Bearer {VAPI_API_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"vapi rejected call: {resp.status_code} {resp.text[:200]}")
            data = resp.json()
        return {"call_id": data.get("id"), "vendor": "vapi"}

    raise RuntimeError(f"unsupported VOICE_VENDOR: {VENDOR!r}")


async def _vendor_get_call(call_id: str) -> dict[str, Any]:
    if VENDOR == "retell":
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.retellai.com/v2/get-call/{call_id}",
                headers={"Authorization": f"Bearer {RETELL_API_KEY}"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"retell get_call failed: {resp.status_code}")
            return resp.json()
    if VENDOR == "vapi":
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.vapi.ai/call/{call_id}",
                headers={"Authorization": f"Bearer {VAPI_API_KEY}"},
            )
            if resp.status_code >= 400:
                raise RuntimeError(f"vapi get_call failed: {resp.status_code}")
            return resp.json()
    raise RuntimeError(f"unsupported VOICE_VENDOR: {VENDOR!r}")


def _normalise_transcript(raw: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten vendor-specific transcript shapes to ``[{role, text}, …]``."""
    if VENDOR == "retell":
        transcript = raw.get("transcript")
        if isinstance(transcript, list):
            return [
                {"role": str(t.get("role") or "unknown"), "text": str(t.get("content") or "")}
                for t in transcript
            ]
        # Fall back to the string transcript when available.
        if isinstance(transcript, str):
            return [{"role": "transcript", "text": transcript}]
    if VENDOR == "vapi":
        messages = raw.get("messages") or []
        out: list[dict[str, str]] = []
        for m in messages:
            if not isinstance(m, dict):
                continue
            role = str(m.get("role") or "unknown")
            text = str(m.get("message") or m.get("content") or "")
            if text:
                out.append({"role": role, "text": text})
        return out
    return []


# ---------------------------------------------------------------------------
# Scoring (independent of vendor)
# ---------------------------------------------------------------------------


async def _score_transcript(
    transcript: list[dict[str, str]],
    rubric: list[str],
) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured for scoring")
    rubric_lines = "\n".join(f"- {r}" for r in rubric)
    flat_transcript = "\n".join(f"{t['role']}: {t['text']}" for t in transcript)
    system = (
        "You are a strict HR interview rater. Score the candidate on each "
        "rubric dimension from 0 to 100 based ONLY on the transcript. "
        "Return STRICT JSON with this shape: "
        '{"scores": {"<dim>": int, ...}, "overall": int, "rationale": "<str>"}.'
    )
    user = f"Rubric:\n{rubric_lines}\n\nTranscript:\n{flat_transcript}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": SCORING_MODEL,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"scoring LLM failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
    content = data["choices"][0]["message"]["content"]
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"scoring LLM returned non-JSON: {exc}") from exc
    return parsed


# ---------------------------------------------------------------------------
# MCP wire protocol — minimal JSONRPC 2 over SSE.
# Keeps us free of the heavyweight MCP SDK while still being protocol-
# compatible with ``dynamiq.connections.MCPSse`` (which expects
# ``initialize`` / ``tools/list`` / ``tools/call`` JSONRPC methods).
# ---------------------------------------------------------------------------


def _jsonrpc_ok(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_err(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _dispatch(req: dict[str, Any]) -> dict[str, Any]:
    method = req.get("method")
    params = req.get("params") or {}
    req_id = req.get("id")

    if method == "initialize":
        return _jsonrpc_ok(
            req_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "voice-mcp", "version": "0.1.0"},
            },
        )

    if method == "tools/list":
        return _jsonrpc_ok(req_id, {"tools": _TOOLS})

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        try:
            if name == "start_interview":
                out = await _vendor_start_call(args)
            elif name == "get_interview_status":
                raw = await _vendor_get_call(args["call_id"])
                out = {
                    "call_id": args["call_id"],
                    "status": raw.get("call_status") or raw.get("status"),
                    "duration_ms": raw.get("duration_ms") or raw.get("duration"),
                    "raw": raw,
                }
            elif name == "get_interview_transcript":
                raw = await _vendor_get_call(args["call_id"])
                out = {"call_id": args["call_id"], "turns": _normalise_transcript(raw)}
            elif name == "score_interview":
                raw = await _vendor_get_call(args["call_id"])
                turns = _normalise_transcript(raw)
                rubric = list(args.get("rubric") or _DEFAULT_RUBRIC)
                out = await _score_transcript(turns, rubric)
                out["call_id"] = args["call_id"]
            else:
                return _jsonrpc_err(req_id, -32601, f"unknown tool: {name}")
        except Exception as exc:  # noqa: BLE001
            return _jsonrpc_err(req_id, -32000, str(exc))
        return _jsonrpc_ok(
            req_id,
            {"content": [{"type": "text", "text": json.dumps(out)}], "isError": False},
        )

    return _jsonrpc_err(req_id, -32601, f"unknown method: {method}")


# ---------------------------------------------------------------------------
# FastAPI app + transports.
# ``/sse`` opens a long-lived stream; clients POST individual JSONRPC messages
# to ``/messages``. This matches the shape ``MCPSse`` consumes.
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield


app = FastAPI(title="voice-mcp", lifespan=lifespan)


def _check_auth(request: Request) -> None:
    if not BEARER:
        return
    got = request.headers.get("authorization", "")
    if got != f"Bearer {BEARER}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="bad token")


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "vendor": VENDOR}


# Per-connection inbound queue keyed by a session id passed back in SSE.
_SESSIONS: dict[str, asyncio.Queue] = {}


@app.get("/sse")
async def sse(request: Request):
    _check_auth(request)
    session_id = f"s_{id(asyncio.current_task())}"
    q: asyncio.Queue = asyncio.Queue()
    _SESSIONS[session_id] = q

    async def event_gen():
        # The "endpoint" event tells the client where to POST inbound msgs.
        yield {"event": "endpoint", "data": f"/messages?session_id={session_id}"}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "message", "data": json.dumps(msg)}
        finally:
            _SESSIONS.pop(session_id, None)

    return EventSourceResponse(event_gen())


@app.post("/messages")
async def messages(request: Request, session_id: str):
    _check_auth(request)
    q = _SESSIONS.get(session_id)
    if q is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown session")
    payload = await request.json()
    out = await _dispatch(payload)
    await q.put(out)
    return JSONResponse({"ok": True})


__all__ = ["app"]
