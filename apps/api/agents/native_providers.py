"""Native Dynamiq provider catalog (Phase A).

Each entry describes a provider that can be connected directly to its
``dynamiq.connections.*`` class — no Composio hop. The catalog is the single
source of truth for:

- the form fields the UI renders in the Connect modal
- credential decoding into a live Dynamiq Connection
- tool-slug → Dynamiq tool node resolution at workflow execution time
- the credential probe used by the "Test" button
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ProviderField:
    """One form field on a Connect modal."""

    key: str
    label: str
    type: str  # "string" | "secret" | "url"
    required: bool = True
    placeholder: str | None = None
    help_text: str | None = None


@dataclass(frozen=True)
class NativeProvider:
    """Catalog entry describing a directly-connectable Dynamiq provider."""

    id: str
    name: str
    category: str  # "search" | "scraping" | "llm" | "mcp" | "oauth"
    description: str
    auth_type: str  # mirrors NativeConnectionAuthType
    fields: tuple[ProviderField, ...]
    tool_slugs: tuple[str, ...]
    icon: str
    docs_url: str | None = None
    # build_connection(config) -> live dynamiq.connections.* instance
    build_connection: Callable[[dict[str, Any]], Any] | None = None
    # probe(connection) -> raises on auth failure; returns short status string
    probe: Callable[[Any], str] | None = None
    # build_tool(connection, slug) -> live dynamiq.nodes.tools.* node, or None to fall through
    build_tool: Callable[[Any, str], Any] | None = None


# ---------------------------------------------------------------------------
# Connection builders
# ---------------------------------------------------------------------------


def _tavily_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Tavily

    return Tavily(api_key=str(cfg["api_key"]))


def _exa_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Exa

    return Exa(api_key=str(cfg["api_key"]))


def _firecrawl_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Firecrawl

    return Firecrawl(api_key=str(cfg["api_key"]))


def _scaleserp_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import ScaleSerp

    return ScaleSerp(api_key=str(cfg["api_key"]))


def _serpapi_conn(cfg: dict[str, Any]) -> Any:
    # SerpApi.com — Dynamiq has no first-class class for it, so use a thin
    # ``Http`` connection that the SerpApiSearch tool below wraps.
    from dynamiq.connections import Http, HTTPMethod

    return Http(
        url="https://serpapi.com/search.json",
        method=HTTPMethod.GET,
        params={"api_key": str(cfg["api_key"])},
    )


def _openai_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import OpenAI

    return OpenAI(api_key=str(cfg["api_key"]))


def _anthropic_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Anthropic

    return Anthropic(api_key=str(cfg["api_key"]))


def _mcp_conn(cfg: dict[str, Any]) -> Any:
    """Generic MCP server connection over SSE."""
    from dynamiq.connections import MCPSse

    headers: dict[str, Any] = {}
    name = (cfg.get("auth_header_name") or "").strip()
    value = (cfg.get("auth_header_value") or "").strip()
    if name and value:
        headers[name] = value
    return MCPSse(
        url=str(cfg["url"]),
        headers=headers or None,
        timeout=float(cfg.get("timeout") or 8.0),
    )


# ---- Twilio Programmable Voice (REST) --------------------------------------
#
# Twilio exposes per-account REST endpoints under
# https://api.twilio.com/2010-04-01/Accounts/{Account SID}/...  We wrap that
# base URL in a plain ``Http`` connection with HTTP Basic auth so any of the
# action slugs (``twilio_call_create``, ``twilio_message_create``, etc.)
# resolves to an ``HttpApiCall`` agents can drive with a JSON body. The
# microservice that actually orchestrates a real conversational interview
# lives elsewhere (an MCP server registered through the ``mcp`` provider).


def _twilio_conn(cfg: dict[str, Any]) -> Any:
    from base64 import b64encode

    from dynamiq.connections import Http, HTTPMethod

    sid = str(cfg.get("account_sid") or "").strip()
    token = str(cfg.get("auth_token") or "").strip()
    if not sid or not token:
        raise ValueError("twilio requires account_sid and auth_token")
    basic = b64encode(f"{sid}:{token}".encode()).decode()
    base = f"https://api.twilio.com/2010-04-01/Accounts/{sid}"
    return Http(
        url=base,
        method=HTTPMethod.POST,
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )


def _twilio_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"Twilio Programmable Voice/SMS action `{slug}` — pass form-encoded "
            "body params. Common slugs: twilio_call_create (POST /Calls.json), "
            "twilio_message_create (POST /Messages.json)."
        ),
        connection=conn,
    )


def _probe_twilio(conn: Any) -> str:
    # /AccountSid.json — cheapest authenticated GET on the account.
    import httpx

    base = conn.url
    with httpx.Client(timeout=10.0) as client:
        resp = client.get(f"{base}.json", headers=dict(conn.headers or {}))
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} {resp.text[:200]}")
    return "ok"


# ---- SendGrid (transactional email) ----------------------------------------


def _sendgrid_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Http, HTTPMethod

    return Http(
        url="https://api.sendgrid.com/v3",
        method=HTTPMethod.POST,
        headers={
            "Authorization": f"Bearer {str(cfg['api_key']).strip()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )


def _sendgrid_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"SendGrid v3 action `{slug}` — pass JSON body. "
            "Common slugs: sendgrid_send (POST /mail/send), "
            "sendgrid_template_send (POST /mail/send with template_id)."
        ),
        connection=conn,
    )


def _probe_sendgrid(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.sendgrid.com/v3/scopes",
        headers={"Authorization": conn.headers.get("Authorization", "")},
    )


# ---- ElevenLabs (multilingual TTS) ----------------------------------------


def _elevenlabs_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import ElevenLabs

    # Dynamiq's ElevenLabs is an Http subclass with the API key in headers.
    return ElevenLabs(api_key=str(cfg["api_key"]).strip())


def _elevenlabs_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"ElevenLabs action `{slug}` — typical slugs: "
            "elevenlabs_tts (POST /v1/text-to-speech/{voice_id}), "
            "elevenlabs_voices_list (GET /v1/voices)."
        ),
        connection=conn,
    )


def _probe_elevenlabs(conn: Any) -> str:
    # ElevenLabs uses xi-api-key header — pull it out of conn.headers.
    headers = dict(conn.headers or {})
    return _probe_via_http_get("https://api.elevenlabs.io/v1/voices", headers=headers)


# ---- OpenAI Whisper (STT) ------------------------------------------------
#
# Whisper is reachable through the standard OpenAI audio endpoint, so the
# connection is just a bearer-auth Http. A separate provider entry keeps the
# UX clean (the user can rotate the Whisper key independently of the chat key).


def _whisper_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Http, HTTPMethod

    return Http(
        url="https://api.openai.com/v1",
        method=HTTPMethod.POST,
        headers={
            "Authorization": f"Bearer {str(cfg['api_key']).strip()}",
            "Accept": "application/json",
        },
    )


def _whisper_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"OpenAI Whisper action `{slug}` — typical: "
            "whisper_transcribe (POST /audio/transcriptions, multipart), "
            "whisper_translate (POST /audio/translations)."
        ),
        connection=conn,
    )


def _probe_whisper(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": conn.headers.get("Authorization", "")},
    )


# ---- Generic HTTP-bearer (Darwin Box, custom REST APIs) -------------------
#
# Catches the long tail. The user pastes a base URL and a bearer token and
# every slug becomes an ``HttpApiCall`` — the agent chooses path + verb on
# the fly through its prompt.


def _http_bearer_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Http, HTTPMethod

    base = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("http_bearer requires base_url")
    token = str(cfg.get("token") or "").strip()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return Http(url=base, method=HTTPMethod.POST, headers=headers)


def _http_bearer_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"Generic REST action `{slug}` — pass a JSON body. Use this for "
            "Darwin Box, internal HR systems, or any bearer-auth REST API. "
            "The agent must include the full path in the request body via "
            "an `additional_path` parameter or as part of the URL."
        ),
        connection=conn,
    )


def _probe_http_bearer(conn: Any) -> str:
    import httpx

    with httpx.Client(timeout=10.0) as client:
        try:
            resp = client.get(conn.url, headers=dict(conn.headers or {}))
        except httpx.RequestError as exc:
            raise RuntimeError(f"cannot reach {conn.url}: {exc}") from exc
    # Anything non-5xx means we reached the server with a valid auth header
    # (most APIs return 200 or 404 on a base GET, both fine).
    if resp.status_code >= 500:
        raise RuntimeError(f"server error {resp.status_code}")
    if resp.status_code in (401, 403):
        raise RuntimeError(f"auth rejected ({resp.status_code}) — check token")
    return f"reachable ({resp.status_code})"


# ---- PostgreSQL ----------------------------------------------------------


def _postgres_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import PostgreSQL

    return PostgreSQL(
        host=str(cfg["host"]).strip(),
        port=int(cfg.get("port") or 5432),
        database=str(cfg["database"]).strip(),
        user=str(cfg["user"]).strip(),
        password=str(cfg.get("password") or ""),
    )


def _postgres_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.sql_executor import SQLExecutor

    return SQLExecutor(connection=conn)


def _probe_postgres(conn: Any) -> str:
    # Open + close a connection. We avoid querying any specific schema since
    # different databases have different system tables.
    try:
        client = conn.connect()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"could not connect: {exc}") from exc
    try:
        # psycopg2 / psycopg cursors both support ``execute``.
        cur = client.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        cur.close()
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return "ok"


# ---- Pipedream Connect (OAuth2 + ~2500 prebuilt actions) ------------------
#
# Pipedream's Connect API authorises end-users into apps (HubSpot, Zendesk,
# Calendly, Slack, etc.) and lets your platform invoke their actions on the
# user's behalf. Auth is OAuth2 on Pipedream itself; per-app credentials are
# managed inside Pipedream. We register the *Pipedream OAuth* connection
# here and route every tool slug through HttpApiCall against
# https://api.pipedream.com/v1/. Connection bridging happens in the
# OAuth flow (apps/api/services/oauth2_service.py).


def _pipedream_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Http, HTTPMethod

    token = str(cfg.get("access_token") or "").strip()
    return Http(
        url="https://api.pipedream.com/v1",
        method=HTTPMethod.POST,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )


def _pipedream_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=(
            f"Pipedream Connect action `{slug}` — pass a JSON body matching "
            "the action's schema. Pipedream resolves the underlying SaaS app "
            "(Calendly, HubSpot, Zendesk, etc.) per the slug prefix."
        ),
        connection=conn,
    )


def _probe_pipedream(conn: Any) -> str:
    token = conn.headers.get("Authorization", "")[len("Bearer ") :]
    return _probe_via_http_get(
        "https://api.pipedream.com/v1/users/me",
        headers={"Authorization": f"Bearer {token}"},
    )


# ---- OAuth providers ------------------------------------------------------
#
# OAuth tools resolve to ``HttpApiCall`` nodes preconfigured with the access
# token. The access token lives in the connection config (refreshed lazily by
# the bridge before tool instantiation when expired).


def _bearer_http_conn(cfg: dict[str, Any]) -> Any:
    from dynamiq.connections import Http, HTTPMethod

    token = str(cfg.get("access_token") or "")
    return Http(
        url=str(cfg.get("_base_url") or ""),
        method=HTTPMethod.POST,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )


def _gmail_conn(cfg: dict[str, Any]) -> Any:
    cfg = dict(cfg)
    cfg["_base_url"] = "https://gmail.googleapis.com"
    return _bearer_http_conn(cfg)


def _slack_conn(cfg: dict[str, Any]) -> Any:
    cfg = dict(cfg)
    cfg["_base_url"] = "https://slack.com/api"
    return _bearer_http_conn(cfg)


def _jira_conn(cfg: dict[str, Any]) -> Any:
    cfg = dict(cfg)
    cloud_id = str(cfg.get("cloud_id") or "")
    cfg["_base_url"] = (
        f"https://api.atlassian.com/ex/jira/{cloud_id}" if cloud_id else "https://api.atlassian.com"
    )
    return _bearer_http_conn(cfg)


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def _tavily_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.tavily import TavilyTool

    return TavilyTool(connection=conn)


def _exa_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.exa_search import ExaTool

    return ExaTool(connection=conn)


def _firecrawl_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.firecrawl import FirecrawlTool

    return FirecrawlTool(connection=conn)


def _scaleserp_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.scale_serp import ScaleSerpTool

    return ScaleSerpTool(connection=conn)


def _serpapi_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name="serpapi-search",
        description="Google SERP via SerpApi.com — pass {query} to search Google and return organic results.",
        connection=conn,
    )


def _mcp_tool(conn: Any, slug: str) -> Any:  # noqa: ARG001
    """Build an MCPServer node that exposes every tool the server advertises."""
    from dynamiq.nodes.tools.mcp import MCPServer

    return MCPServer(connection=conn)


# OAuth tool builders — each emits an HttpApiCall preconfigured for one action.
# More actions per provider can be added later; the interpreter advertises the
# slug list via ``tool_slugs`` so agents know to emit them.


def _gmail_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    # ``slug`` is one of: gmail_send, gmail_list_messages, gmail_get_message
    return HttpApiCall(
        name=slug,
        description=f"Gmail API action `{slug}` — pass body params as JSON.",
        connection=conn,
    )


def _slack_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=f"Slack Web API action `{slug}` — pass body params as JSON.",
        connection=conn,
    )


def _jira_tool(conn: Any, slug: str) -> Any:
    from dynamiq.nodes.tools.http_api_call import HttpApiCall

    return HttpApiCall(
        name=slug,
        description=f"Jira Cloud REST v3 action `{slug}` — pass body params as JSON.",
        connection=conn,
    )


# OpenAI / Anthropic don't expose stand-alone "search tool" nodes — they're LLM
# backends. Agents already use the runtime-resolved LLM. Listing them here
# means the user can register a workspace-level API key that the LLM resolver
# may consume (Phase 2 will rewire ``_resolve_llm`` to prefer these); the tool
# builders below return ``None`` so the bridge skips them as agent tools.


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------


def _probe_via_http_get(url: str, headers: dict[str, str] | None = None) -> str:
    import httpx

    with httpx.Client(timeout=10.0) as client:
        resp = client.get(url, headers=headers or {})
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} {resp.text[:160]}")
    return "ok"


def _probe_tavily(conn: Any) -> str:
    import httpx

    with httpx.Client(timeout=10.0) as client:
        resp = client.post(
            "https://api.tavily.com/search",
            json={"api_key": conn.api_key, "query": "ping", "max_results": 1},
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"{resp.status_code} {resp.text[:160]}")
    return "ok"


def _probe_exa(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.exa.ai/search",
        headers={"x-api-key": conn.api_key, "accept": "application/json"},
    )


def _probe_firecrawl(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.firecrawl.dev/v1/team/credit-usage",
        headers={"Authorization": f"Bearer {conn.api_key}"},
    )


def _probe_scaleserp(conn: Any) -> str:
    return _probe_via_http_get(f"https://api.scaleserp.com/account?api_key={conn.api_key}")


def _probe_serpapi(conn: Any) -> str:
    return _probe_via_http_get(
        f"https://serpapi.com/account.json?api_key={conn.params['api_key']}"
    )


def _probe_openai(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {conn.api_key}"},
    )


def _probe_anthropic(conn: Any) -> str:
    return _probe_via_http_get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": conn.api_key, "anthropic-version": "2023-06-01"},
    )


def _probe_oauth_bearer_get(url: str, token: str) -> str:
    return _probe_via_http_get(url, headers={"Authorization": f"Bearer {token}"})


def _probe_gmail(conn: Any) -> str:
    token = (conn.headers or {}).get("Authorization", "")[len("Bearer ") :]
    return _probe_oauth_bearer_get(
        "https://gmail.googleapis.com/gmail/v1/users/me/profile", token
    )


def _probe_slack(conn: Any) -> str:
    token = (conn.headers or {}).get("Authorization", "")[len("Bearer ") :]
    return _probe_oauth_bearer_get("https://slack.com/api/auth.test", token)


def _probe_jira(conn: Any) -> str:
    token = (conn.headers or {}).get("Authorization", "")[len("Bearer ") :]
    return _probe_oauth_bearer_get(
        "https://api.atlassian.com/oauth/token/accessible-resources", token
    )


def _probe_mcp(conn: Any) -> str:
    """Cheap reachability probe — HEAD the SSE URL with the configured headers."""
    import httpx

    headers = dict(conn.headers or {})
    headers.setdefault("Accept", "text/event-stream")
    with httpx.Client(timeout=conn.timeout or 8.0) as client:
        # GET with stream=True wouldn't help here — we just want to know the
        # endpoint exists and authorizes us. Most MCP servers will 405 a HEAD
        # but return 200 / 401 for an SSE GET; either non-5xx is good.
        try:
            resp = client.get(conn.url, headers=headers)
        except httpx.RequestError as exc:
            raise RuntimeError(f"cannot reach {conn.url}: {exc}") from exc
        if resp.status_code >= 500:
            raise RuntimeError(f"server error {resp.status_code} {resp.text[:160]}")
        if resp.status_code in (401, 403):
            raise RuntimeError(f"auth rejected ({resp.status_code}) — check auth header")
    return f"reachable ({resp.status_code})"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


_API_KEY_FIELD = ProviderField(
    key="api_key",
    label="API key",
    type="secret",
    required=True,
    placeholder="paste your provider API key",
)


_CATALOG: tuple[NativeProvider, ...] = (
    NativeProvider(
        id="tavily",
        name="Tavily",
        category="search",
        description="LLM-friendly web search API with citation-ready results.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("tavily-search", "web_search", "tavily"),
        icon="search",
        docs_url="https://tavily.com",
        build_connection=_tavily_conn,
        probe=_probe_tavily,
        build_tool=_tavily_tool,
    ),
    NativeProvider(
        id="exa",
        name="Exa",
        category="search",
        description="Neural search over the web — semantic queries, full page contents.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("exa-search", "exa", "neural_search"),
        icon="search",
        docs_url="https://exa.ai",
        build_connection=_exa_conn,
        probe=_probe_exa,
        build_tool=_exa_tool,
    ),
    NativeProvider(
        id="firecrawl",
        name="Firecrawl",
        category="scraping",
        description="LLM-ready web scraping & crawling — markdown / structured output.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("firecrawl", "firecrawl-scrape", "web_scrape"),
        icon="globe",
        docs_url="https://firecrawl.dev",
        build_connection=_firecrawl_conn,
        probe=_probe_firecrawl,
        build_tool=_firecrawl_tool,
    ),
    NativeProvider(
        id="scaleserp",
        name="ScaleSerp",
        category="search",
        description="Google SERP scraping — search, news, places, shopping.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("scale-serp", "scaleserp", "google_serp"),
        icon="search",
        docs_url="https://www.scaleserp.com",
        build_connection=_scaleserp_conn,
        probe=_probe_scaleserp,
        build_tool=_scaleserp_tool,
    ),
    NativeProvider(
        id="serpapi",
        name="SerpApi",
        category="search",
        description="Real-time Google / Bing / Yandex SERP API.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("serpapi", "serpapi-search"),
        icon="search",
        docs_url="https://serpapi.com",
        build_connection=_serpapi_conn,
        probe=_probe_serpapi,
        build_tool=_serpapi_tool,
    ),
    NativeProvider(
        id="openai",
        name="OpenAI",
        category="llm",
        description="GPT-4o / GPT-5 / o-series chat completions. Used by agents as LLM backend.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=(),
        icon="cpu",
        docs_url="https://platform.openai.com",
        build_connection=_openai_conn,
        probe=_probe_openai,
        build_tool=None,
    ),
    NativeProvider(
        id="anthropic",
        name="Anthropic",
        category="llm",
        description="Claude Opus / Sonnet / Haiku. Used by agents as LLM backend.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=(),
        icon="cpu",
        docs_url="https://console.anthropic.com",
        build_connection=_anthropic_conn,
        probe=_probe_anthropic,
        build_tool=None,
    ),
    NativeProvider(
        id="gmail",
        name="Gmail",
        category="oauth",
        description="Send messages and read inbox via Google OAuth.",
        auth_type="oauth2",
        fields=(),  # no manual fields — flow is OAuth redirect
        tool_slugs=("gmail_send", "gmail_list_messages", "gmail_get_message"),
        icon="globe",
        docs_url="https://developers.google.com/gmail/api",
        build_connection=_gmail_conn,
        probe=_probe_gmail,
        build_tool=_gmail_tool,
    ),
    NativeProvider(
        id="slack",
        name="Slack",
        category="oauth",
        description="Post messages and read channels via Slack OAuth (bot scope).",
        auth_type="oauth2",
        fields=(),
        tool_slugs=("slack_post_message", "slack_list_channels", "slack_user_info"),
        icon="globe",
        docs_url="https://api.slack.com/apps",
        build_connection=_slack_conn,
        probe=_probe_slack,
        build_tool=_slack_tool,
    ),
    NativeProvider(
        id="jira",
        name="Jira",
        category="oauth",
        description="Create / read issues and search via Atlassian OAuth.",
        auth_type="oauth2",
        fields=(),
        tool_slugs=("jira_create_issue", "jira_search_issues", "jira_get_issue"),
        icon="globe",
        docs_url="https://developer.atlassian.com/cloud/jira/platform/oauth-2-3lo-apps/",
        build_connection=_jira_conn,
        probe=_probe_jira,
        build_tool=_jira_tool,
    ),
    NativeProvider(
        id="twilio",
        name="Twilio",
        category="telephony",
        description=(
            "Programmable Voice & SMS — place outbound calls, send SMS, run "
            "TwiML flows. For real-time conversational interviews, register a "
            "Retell/Vapi MCP server in addition (this provider is for direct "
            "REST actions)."
        ),
        auth_type="api_key",
        fields=(
            ProviderField(
                key="account_sid",
                label="Account SID",
                type="string",
                required=True,
                placeholder="ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            ),
            ProviderField(
                key="auth_token",
                label="Auth token",
                type="secret",
                required=True,
                placeholder="paste your Twilio auth token",
            ),
        ),
        tool_slugs=(
            "twilio_call_create",
            "twilio_message_create",
            "twilio_call_status",
            "twilio_recording_fetch",
        ),
        icon="phone",
        docs_url="https://www.twilio.com/docs/voice/api",
        build_connection=_twilio_conn,
        probe=_probe_twilio,
        build_tool=_twilio_tool,
    ),
    NativeProvider(
        id="sendgrid",
        name="SendGrid",
        category="email",
        description="Transactional email with templates, tracking, and webhooks.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=(
            "sendgrid_send",
            "sendgrid_template_send",
            "sendgrid_stats",
        ),
        icon="mail",
        docs_url="https://docs.sendgrid.com/api-reference",
        build_connection=_sendgrid_conn,
        probe=_probe_sendgrid,
        build_tool=_sendgrid_tool,
    ),
    NativeProvider(
        id="elevenlabs",
        name="ElevenLabs",
        category="voice",
        description=(
            "Multilingual TTS. Use for generating interview prompts in the "
            "candidate's preferred language."
        ),
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("elevenlabs_tts", "elevenlabs_voices_list"),
        icon="mic",
        docs_url="https://elevenlabs.io/docs",
        build_connection=_elevenlabs_conn,
        probe=_probe_elevenlabs,
        build_tool=_elevenlabs_tool,
    ),
    NativeProvider(
        id="whisper",
        name="OpenAI Whisper",
        category="voice",
        description="STT — transcribe interview audio in any major language.",
        auth_type="api_key",
        fields=(_API_KEY_FIELD,),
        tool_slugs=("whisper_transcribe", "whisper_translate"),
        icon="mic",
        docs_url="https://platform.openai.com/docs/guides/speech-to-text",
        build_connection=_whisper_conn,
        probe=_probe_whisper,
        build_tool=_whisper_tool,
    ),
    NativeProvider(
        id="http_bearer",
        name="Generic HTTP (Bearer)",
        category="custom",
        description=(
            "Catch-all bearer-auth REST connector. Use for Darwin Box, "
            "internal HR systems, custom ticketing APIs, anything with a "
            "single static token."
        ),
        auth_type="api_key",
        fields=(
            ProviderField(
                key="base_url",
                label="Base URL",
                type="url",
                required=True,
                placeholder="https://yourcompany.darwinbox.in/api",
            ),
            ProviderField(
                key="token",
                label="Bearer token (optional)",
                type="secret",
                required=False,
                placeholder="paste your access token",
                help_text="Leave blank if the API is public or uses cookies.",
            ),
        ),
        tool_slugs=(
            "http_post",
            "http_get",
            "darwinbox_resume_search",
            "darwinbox_candidate_get",
        ),
        icon="globe",
        docs_url=None,
        build_connection=_http_bearer_conn,
        probe=_probe_http_bearer,
        build_tool=_http_bearer_tool,
    ),
    NativeProvider(
        id="ats",
        name="ATS / Résumé Source",
        category="custom",
        description=(
            "Applicant-tracking / HRIS résumé source for the recruitment "
            "templates. A bearer-auth REST connector: point ``base_url`` at your "
            "ATS's candidate-search endpoint. The ``ats_search_candidates`` "
            "action POSTs ``{jd, role, limit}`` and MUST return a JSON list (or "
            "``{data: [...]}``) of candidates with at least "
            "``candidate_id, name, email, phone``. In draft/demo runs a sample "
            "shortlist is returned so the pipeline is testable without a live ATS."
        ),
        auth_type="api_key",
        fields=(
            ProviderField(
                key="base_url",
                label="ATS search endpoint",
                type="url",
                required=True,
                placeholder="https://yourcompany.example.com/api/candidates/search",
            ),
            ProviderField(
                key="token",
                label="Bearer token",
                type="secret",
                required=False,
                placeholder="paste your ATS API token",
            ),
        ),
        tool_slugs=("ats_search_candidates",),
        icon="users",
        docs_url=None,
        build_connection=_http_bearer_conn,
        probe=_probe_http_bearer,
        build_tool=_http_bearer_tool,
    ),
    NativeProvider(
        id="postgres",
        name="PostgreSQL",
        category="database",
        description=(
            "Run SQL against any Postgres instance. Useful for backing demos "
            "with a workspace-local customers/tickets/candidates table."
        ),
        auth_type="api_key",
        fields=(
            ProviderField(
                key="host", label="Host", type="string", required=True,
                placeholder="db.example.com",
            ),
            ProviderField(
                key="port", label="Port", type="string", required=False,
                placeholder="5432",
            ),
            ProviderField(
                key="database", label="Database", type="string", required=True,
                placeholder="postgres",
            ),
            ProviderField(
                key="user", label="User", type="string", required=True,
                placeholder="postgres",
            ),
            ProviderField(
                key="password", label="Password", type="secret", required=False,
                placeholder="••••••••",
            ),
        ),
        tool_slugs=("sql_query", "sql_execute", "postgres"),
        icon="database",
        docs_url="https://www.postgresql.org/docs/",
        build_connection=_postgres_conn,
        probe=_probe_postgres,
        build_tool=_postgres_tool,
    ),
    NativeProvider(
        id="pipedream",
        name="Pipedream Connect",
        category="oauth",
        description=(
            "Unlocks ~2,500 prebuilt SaaS actions (HubSpot, Zendesk, Calendly, "
            "Cal.com, Notion, Airtable, Stripe, Twilio, etc.) through a single "
            "OAuth grant against Pipedream."
        ),
        auth_type="oauth2",
        fields=(),
        tool_slugs=(
            "pipedream_run_action",
            "pipedream_calendly_create_event",
            "pipedream_hubspot_create_contact",
            "pipedream_zendesk_create_ticket",
        ),
        icon="globe",
        docs_url="https://pipedream.com/docs/connect/",
        build_connection=_pipedream_conn,
        probe=_probe_pipedream,
        build_tool=_pipedream_tool,
    ),
    NativeProvider(
        id="mcp",
        name="MCP Server",
        category="mcp",
        description="Connect any Model Context Protocol server over SSE — the server's "
        "advertised tools become available to agents automatically.",
        auth_type="mcp_sse",
        fields=(
            ProviderField(
                key="url",
                label="SSE endpoint URL",
                type="url",
                required=True,
                placeholder="https://your-mcp-server.example.com/sse",
            ),
            ProviderField(
                key="auth_header_name",
                label="Auth header name (optional)",
                type="string",
                required=False,
                placeholder="Authorization",
            ),
            ProviderField(
                key="auth_header_value",
                label="Auth header value (optional)",
                type="secret",
                required=False,
                placeholder="Bearer ey...",
                help_text="Leave both auth fields blank if your MCP server is public.",
            ),
        ),
        tool_slugs=("mcp", "mcp_server", "mcp-tools"),
        icon="globe",
        docs_url="https://modelcontextprotocol.io",
        build_connection=_mcp_conn,
        probe=_probe_mcp,
        build_tool=_mcp_tool,
    ),
)


_BY_ID: dict[str, NativeProvider] = {p.id: p for p in _CATALOG}


def list_providers() -> tuple[NativeProvider, ...]:
    return _CATALOG


def get_provider(provider_id: str) -> NativeProvider | None:
    return _BY_ID.get(provider_id.strip().lower())


def resolve_provider_for_slug(slug: str) -> NativeProvider | None:
    """Find the provider that owns a given tool slug (case-insensitive)."""
    target = slug.strip().lower()
    for p in _CATALOG:
        for s in p.tool_slugs:
            if s.lower() == target:
                return p
    return None


__all__ = [
    "NativeProvider",
    "ProviderField",
    "list_providers",
    "get_provider",
    "resolve_provider_for_slug",
]
