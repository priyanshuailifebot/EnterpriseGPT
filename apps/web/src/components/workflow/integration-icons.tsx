"use client";

/**
 * Slug → provider icon resolver for the visual editor.
 *
 * Mirrors the catalog in ``apps/api/agents/native_providers.py`` and the
 * Composio bridge map in ``apps/api/egpt_mcp/provider_apps.py``. The
 * client-side copy avoids a network round-trip per node — the catalog
 * changes rarely enough that drift is preferable to flicker.
 *
 * Brand icons are intentionally not real logos (license + bundle-size
 * cost). Instead each provider gets a lucide icon paired with a brand
 * color so the eye reads them as "different integrations" at a glance.
 * When a node has multiple tools spanning multiple providers, the editor
 * stacks every distinct icon — the most common pattern is 1-2.
 */

import type { ReactNode } from "react";
import {
  Cpu,
  Database,
  FileSearch,
  Globe,
  Hash,
  HelpCircle,
  Inbox,
  Mail,
  MessageSquare,
  Mic,
  Network,
  Phone,
  Search,
  Send,
  ServerCog,
  Sparkles,
  Ticket,
  Workflow,
} from "lucide-react";

export interface ProviderTag {
  /** Stable id matching native_providers.py ids. */
  id: string;
  label: string;
  /** Tailwind background tint applied to the chip. */
  bg: string;
  /** Tailwind foreground colour. */
  fg: string;
  icon: ReactNode;
}

// ---------------------------------------------------------------------------
// Provider table — kept flat & literal for tree-shake friendliness.
// ---------------------------------------------------------------------------

const _PROVIDERS: Record<string, ProviderTag> = {
  // ---- Native / direct ----
  tavily: {
    id: "tavily",
    label: "Tavily",
    bg: "bg-emerald-100 dark:bg-emerald-950",
    fg: "text-emerald-700 dark:text-emerald-300",
    icon: <Search className="h-3.5 w-3.5" />,
  },
  exa: {
    id: "exa",
    label: "Exa",
    bg: "bg-violet-100 dark:bg-violet-950",
    fg: "text-violet-700 dark:text-violet-300",
    icon: <Search className="h-3.5 w-3.5" />,
  },
  firecrawl: {
    id: "firecrawl",
    label: "Firecrawl",
    bg: "bg-orange-100 dark:bg-orange-950",
    fg: "text-orange-700 dark:text-orange-300",
    icon: <Globe className="h-3.5 w-3.5" />,
  },
  scaleserp: {
    id: "scaleserp",
    label: "ScaleSerp",
    bg: "bg-sky-100 dark:bg-sky-950",
    fg: "text-sky-700 dark:text-sky-300",
    icon: <Search className="h-3.5 w-3.5" />,
  },
  serpapi: {
    id: "serpapi",
    label: "SerpApi",
    bg: "bg-sky-100 dark:bg-sky-950",
    fg: "text-sky-700 dark:text-sky-300",
    icon: <Search className="h-3.5 w-3.5" />,
  },
  openai: {
    id: "openai",
    label: "OpenAI",
    bg: "bg-slate-100 dark:bg-slate-800",
    fg: "text-slate-700 dark:text-slate-200",
    icon: <Cpu className="h-3.5 w-3.5" />,
  },
  anthropic: {
    id: "anthropic",
    label: "Anthropic",
    bg: "bg-amber-100 dark:bg-amber-950",
    fg: "text-amber-700 dark:text-amber-300",
    icon: <Cpu className="h-3.5 w-3.5" />,
  },
  gmail: {
    id: "gmail",
    label: "Gmail",
    bg: "bg-red-100 dark:bg-red-950",
    fg: "text-red-700 dark:text-red-300",
    icon: <Mail className="h-3.5 w-3.5" />,
  },
  slack: {
    id: "slack",
    label: "Slack",
    bg: "bg-fuchsia-100 dark:bg-fuchsia-950",
    fg: "text-fuchsia-700 dark:text-fuchsia-300",
    icon: <MessageSquare className="h-3.5 w-3.5" />,
  },
  jira: {
    id: "jira",
    label: "Jira",
    bg: "bg-blue-100 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    icon: <Ticket className="h-3.5 w-3.5" />,
  },
  // ---- Phase added in v2 ----
  twilio: {
    id: "twilio",
    label: "Twilio",
    bg: "bg-rose-100 dark:bg-rose-950",
    fg: "text-rose-700 dark:text-rose-300",
    icon: <Phone className="h-3.5 w-3.5" />,
  },
  sendgrid: {
    id: "sendgrid",
    label: "SendGrid",
    bg: "bg-cyan-100 dark:bg-cyan-950",
    fg: "text-cyan-700 dark:text-cyan-300",
    icon: <Send className="h-3.5 w-3.5" />,
  },
  elevenlabs: {
    id: "elevenlabs",
    label: "ElevenLabs",
    bg: "bg-purple-100 dark:bg-purple-950",
    fg: "text-purple-700 dark:text-purple-300",
    icon: <Mic className="h-3.5 w-3.5" />,
  },
  whisper: {
    id: "whisper",
    label: "Whisper",
    bg: "bg-teal-100 dark:bg-teal-950",
    fg: "text-teal-700 dark:text-teal-300",
    icon: <Mic className="h-3.5 w-3.5" />,
  },
  http_bearer: {
    id: "http_bearer",
    label: "HTTP",
    bg: "bg-slate-100 dark:bg-slate-800",
    fg: "text-slate-700 dark:text-slate-200",
    icon: <Network className="h-3.5 w-3.5" />,
  },
  darwinbox: {
    id: "darwinbox",
    label: "Darwin Box",
    bg: "bg-indigo-100 dark:bg-indigo-950",
    fg: "text-indigo-700 dark:text-indigo-300",
    icon: <Inbox className="h-3.5 w-3.5" />,
  },
  postgres: {
    id: "postgres",
    label: "Postgres",
    bg: "bg-blue-100 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    icon: <Database className="h-3.5 w-3.5" />,
  },
  pipedream: {
    id: "pipedream",
    label: "Pipedream",
    bg: "bg-lime-100 dark:bg-lime-950",
    fg: "text-lime-700 dark:text-lime-300",
    icon: <Workflow className="h-3.5 w-3.5" />,
  },
  mcp: {
    id: "mcp",
    label: "MCP",
    bg: "bg-zinc-100 dark:bg-zinc-800",
    fg: "text-zinc-700 dark:text-zinc-200",
    icon: <ServerCog className="h-3.5 w-3.5" />,
  },
  // ---- Composio fallback providers (slug-only mapping is in resolveSlug) ----
  googledrive: {
    id: "googledrive",
    label: "Drive",
    bg: "bg-green-100 dark:bg-green-950",
    fg: "text-green-700 dark:text-green-300",
    icon: <FileSearch className="h-3.5 w-3.5" />,
  },
  googlecalendar: {
    id: "googlecalendar",
    label: "Calendar",
    bg: "bg-blue-100 dark:bg-blue-950",
    fg: "text-blue-700 dark:text-blue-300",
    icon: <Hash className="h-3.5 w-3.5" />,
  },
  googlesheets: {
    id: "googlesheets",
    label: "Sheets",
    bg: "bg-emerald-100 dark:bg-emerald-950",
    fg: "text-emerald-700 dark:text-emerald-300",
    icon: <Hash className="h-3.5 w-3.5" />,
  },
  googlemeet: {
    id: "googlemeet",
    label: "Meet",
    bg: "bg-emerald-100 dark:bg-emerald-950",
    fg: "text-emerald-700 dark:text-emerald-300",
    icon: <Phone className="h-3.5 w-3.5" />,
  },
  servicenow: {
    id: "servicenow",
    label: "ServiceNow",
    bg: "bg-emerald-100 dark:bg-emerald-950",
    fg: "text-emerald-700 dark:text-emerald-300",
    icon: <Ticket className="h-3.5 w-3.5" />,
  },
  retell: {
    id: "retell",
    label: "Voice MCP",
    bg: "bg-violet-100 dark:bg-violet-950",
    fg: "text-violet-700 dark:text-violet-300",
    icon: <Phone className="h-3.5 w-3.5" />,
  },
};

// ---------------------------------------------------------------------------
// Slug → provider resolution.
//
// Mirrors ``native_providers.resolve_provider_for_slug`` plus the Composio
// short-prefix scheme used in workflow templates (`pipedream_zendesk_*` etc).
// ---------------------------------------------------------------------------

const _DIRECT_SLUGS: Record<string, string> = {};

(function buildDirect() {
  // Native catalog slugs — copied verbatim from native_providers.py.
  const map: Record<string, string[]> = {
    tavily: ["tavily-search", "web_search", "tavily"],
    exa: ["exa-search", "exa", "neural_search"],
    firecrawl: ["firecrawl", "firecrawl-scrape", "web_scrape"],
    scaleserp: ["scale-serp", "scaleserp", "google_serp"],
    serpapi: ["serpapi", "serpapi-search"],
    gmail: ["gmail_send", "gmail_list_messages", "gmail_get_message"],
    slack: ["slack_post_message", "slack_list_channels", "slack_user_info"],
    jira: ["jira_create_issue", "jira_search_issues", "jira_get_issue"],
    mcp: ["mcp", "mcp_server", "mcp-tools"],
    twilio: [
      "twilio_call_create",
      "twilio_message_create",
      "twilio_call_status",
      "twilio_recording_fetch",
    ],
    sendgrid: ["sendgrid_send", "sendgrid_template_send", "sendgrid_stats"],
    elevenlabs: ["elevenlabs_tts", "elevenlabs_voices_list"],
    whisper: ["whisper_transcribe", "whisper_translate"],
    http_bearer: ["http_post", "http_get"],
    darwinbox: ["darwinbox_resume_search", "darwinbox_candidate_get"],
    postgres: ["sql_query", "sql_execute", "postgres"],
    pipedream: [
      "pipedream_run_action",
      "pipedream_calendly_create_event",
      "pipedream_hubspot_create_contact",
      "pipedream_zendesk_create_ticket",
    ],
    retell: [
      "start_interview",
      "get_interview_status",
      "get_interview_transcript",
      "score_interview",
    ],
  };
  for (const [pid, slugs] of Object.entries(map)) {
    for (const s of slugs) {
      _DIRECT_SLUGS[s.toLowerCase()] = pid;
    }
  }
})();

export function resolveProviderForSlug(slug: string): ProviderTag | null {
  if (!slug) return null;
  const norm = slug.trim().toLowerCase();

  // 1) Direct match.
  const direct = _DIRECT_SLUGS[norm];
  if (direct && _PROVIDERS[direct]) return _PROVIDERS[direct];

  // 2) Prefix conventions for Pipedream / Composio bridge slugs:
  //    ``pipedream_calendly_*``, ``pipedream_zendesk_*`` etc — still surface
  //    the Pipedream brand since that's the connection backing them.
  if (norm.startsWith("pipedream_")) return _PROVIDERS.pipedream;
  if (norm.startsWith("twilio_")) return _PROVIDERS.twilio;
  if (norm.startsWith("sendgrid_")) return _PROVIDERS.sendgrid;
  if (norm.startsWith("elevenlabs_")) return _PROVIDERS.elevenlabs;
  if (norm.startsWith("whisper_")) return _PROVIDERS.whisper;
  if (norm.startsWith("gmail_")) return _PROVIDERS.gmail;
  if (norm.startsWith("slack_")) return _PROVIDERS.slack;
  if (norm.startsWith("jira_")) return _PROVIDERS.jira;
  if (norm.startsWith("darwinbox_")) return _PROVIDERS.darwinbox;
  if (norm.startsWith("sql_")) return _PROVIDERS.postgres;
  if (norm.startsWith("http_")) return _PROVIDERS.http_bearer;

  return null;
}

export function uniqueProvidersForTools(tools: string[]): ProviderTag[] {
  const seen = new Map<string, ProviderTag>();
  for (const t of tools || []) {
    const p = resolveProviderForSlug(t);
    if (p && !seen.has(p.id)) seen.set(p.id, p);
  }
  return [...seen.values()];
}

export function unknownToolBadge(): ProviderTag {
  return {
    id: "_unknown",
    label: "tool",
    bg: "bg-slate-100 dark:bg-slate-800",
    fg: "text-slate-500 dark:text-slate-400",
    icon: <Sparkles className="h-3.5 w-3.5" />,
  };
}

export function helpBadge(): ProviderTag {
  return {
    id: "_help",
    label: "?",
    bg: "bg-slate-100 dark:bg-slate-800",
    fg: "text-slate-500 dark:text-slate-400",
    icon: <HelpCircle className="h-3.5 w-3.5" />,
  };
}
