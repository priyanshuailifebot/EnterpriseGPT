# Voice-Interview MCP server

A small Model Context Protocol server that wraps **Retell** (default) or
**Vapi** so EnterpriseGPT workflows can place real conversational phone
interviews without owning the telephony / STT / TTS stack.

The service exposes four tools over MCP-SSE:

| Tool | What it does |
| ---- | ------------ |
| `start_interview` | Places an outbound call. Vendor agent runs the conversation. Returns a `call_id` immediately. |
| `get_interview_status` | Polls vendor for call lifecycle (`ringing` → `in_progress` → `ended`). |
| `get_interview_transcript` | Returns a normalised `[{role, text}, …]` transcript. |
| `score_interview` | Zero-temperature LLM scoring against an 8-dimension HR rubric. |

## Why a separate microservice

EnterpriseGPT already supports MCP-SSE connections out of the box (see
`apps/api/agents/native_providers.py` → `mcp`). Once this service is running
and a user registers its URL through the **MCP Server** integration tile,
every workflow can call `start_interview` / `get_interview_status` /
`get_interview_transcript` / `score_interview` as agent tools. No core
platform changes needed to swap vendors — change `VOICE_VENDOR` and restart.

## Local run

```bash
cd infra/voice-mcp
pip install -r requirements.txt

export VOICE_VENDOR=retell           # or "vapi"
export RETELL_API_KEY=...
export RETELL_AGENT_ID=agent_xxx     # Retell agent that owns the system prompt
export RETELL_FROM_NUMBER=+15551234567
export OPENAI_API_KEY=...            # used by score_interview
export VOICE_MCP_BEARER=demo-token   # clients must send Authorization: Bearer demo-token

uvicorn server:app --host 0.0.0.0 --port 8090
```

Then in EnterpriseGPT → Integrations → MCP Server:

* **SSE endpoint URL:** `http://localhost:8090/sse`
* **Auth header name:** `Authorization`
* **Auth header value:** `Bearer demo-token`

The four tools become available to any agent.

## Why Retell by default

Retell exposes a one-shot `create-phone-call` REST endpoint and handles
turn-detection / barge-in / multilingual voice internally. Vapi is the
fallback for teams who already use it — the request shape diverges, the
contract this service exposes does not.
