/** API contract types aligned with apps/api/schemas. */

// ---------------------------------------------------------------------------
// Auth & RBAC (models.user.UserRole / core.permissions)
// ---------------------------------------------------------------------------

export type UserRole =
  | "super_admin"
  | "admin"
  | "builder"
  | "operator"
  | "viewer";

export type Permission =
  | "workflow:create"
  | "workflow:run"
  | "workflow:read"
  | "workflow:delete"
  | "document:upload"
  | "document:read"
  | "user:manage"
  | "workspace:manage"
  | "analytics:read"
  | "mcp:manage";

export interface WorkspaceMembershipResponse {
  workspace_id: string;
  workspace_name: string;
  workspace_slug: string;
  role: UserRole;
}

export interface UserResponse {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  is_active: boolean;
  mfa_enabled: boolean;
  last_login: string | null;
  created_at: string;
  workspaces: WorkspaceMembershipResponse[];
}

export interface LoginRequest {
  email: string;
  password: string;
  totp_code?: string | null;
}

/** POST /api/v1/auth/register — omit `role` for default viewer; backend ignores custom roles outside development. */
export interface RegisterRequest {
  email: string;
  password: string;
  full_name: string;
  role?: UserRole | null;
}

export interface LoginResponse {
  user: UserResponse;
  access_token: string;
  token_type?: string;
  expires_in: number;
}

export interface RefreshResponse {
  access_token: string;
  expires_in: number;
  token_type?: string;
}

// ---------------------------------------------------------------------------
// Workflows — interpret / definitions
// ---------------------------------------------------------------------------

export type ClarificationQuestionType = "text" | "choice" | "multi_choice";

export interface ClarificationQuestion {
  id: string;
  question: string;
  type: ClarificationQuestionType;
  options: string[] | null;
  why_asked: string;
  required: boolean;
}

export interface ClarificationAnswer {
  question_id: string;
  answer: string | string[];
}

export interface InterpretRequestPayload {
  text?: string | null;
  session_id?: string | null;
  answers?: ClarificationAnswer[];
  force_proceed?: boolean;
  skip_clarification?: boolean;
  workspace_id?: string | null;
}

export interface AgentDefinition {
  id: string;
  name: string;
  role: string;
  instructions: string;
  tools: string[];
  depends_on: string[];
  is_parallel: boolean;
  /** v2: gate activation on a specific upstream condition branch. */
  activate_on?: Record<string, string> | null;
}

/** ---- v2 polymorphic node-kind discriminated union ----
 *
 * Definitions that opt into v2 emit a ``nodes`` array. Legacy graphs that
 * only set ``agents`` still validate against the API and the front-end
 * topology helper synthesises an ``AgentNode`` for each entry.
 */

interface _BaseNode {
  id: string;
  name: string;
  depends_on: string[];
  activate_on?: Record<string, string> | null;
}

export interface AgentNode extends _BaseNode {
  kind: "agent";
  role: string;
  instructions: string;
  tools: string[];
  is_parallel: boolean;
  /** Tools-Agent composite — node id of a MemoryNode the agent reads / writes. */
  memory_ref?: string;
  /** Tools-Agent composite — node id of an OutputParserNode. */
  output_parser_ref?: string;
  /** Optional explicit LLM choice (provider + model). */
  chat_model?: AgentChatModel | null;
}

export interface ConditionNode extends _BaseNode {
  kind: "condition";
  expression: string;
  branches: string[];
}

export interface ForEachNode extends _BaseNode {
  kind: "for_each";
  items_from: string;
  items_path: string;
  item_var: string;
  body: string[];
  max_concurrency: number;
}

export interface MergeNode extends _BaseNode {
  kind: "merge";
}

export interface WaitForWebhookNode extends _BaseNode {
  kind: "wait_for_webhook";
  description: string;
  timeout_seconds: number;
  response_schema?: Record<string, unknown> | null;
}

export interface TriggerFormField {
  key: string;
  label: string;
  type: "text" | "choice" | "multi_choice";
  required?: boolean;
  options?: string[];
}

export interface TriggerNode extends _BaseNode {
  kind: "trigger";
  trigger_type: "manual" | "webhook" | "form" | "schedule" | "chat";
  slug: string;
  form_fields: TriggerFormField[];
  schedule_cron: string;
  secret_required: boolean;
  /** Chat-specific. Empty when ``trigger_type !== "chat"``. */
  chat_welcome_message: string;
  chat_memory_ref: string;
}

export interface AgentChatModel {
  provider: string;
  model: string;
  temperature?: number;
}

export interface ActionNode extends _BaseNode {
  kind: "action";
  provider: string;
  action_slug: string;
  params: Record<string, unknown>;
  allow_dry_run: boolean;
  /** When set, this action is a satellite tool of the named agent. */
  parent_agent_id?: string | null;
  /** Human-readable description used by the LLM to decide when to call it. */
  tool_description?: string;
  /** Bind to a specific workspace connection (multi-account providers). */
  connection_id?: string | null;
}

export interface MemoryNode extends _BaseNode {
  kind: "memory";
  scope: "session" | "user" | "workflow";
  store: "redis" | "postgres";
  ttl_seconds: number;
  max_turns: number;
  parent_agent_id?: string | null;
}

export interface OutputParserNode extends _BaseNode {
  kind: "output_parser";
  json_schema: Record<string, unknown>;
  max_retries: number;
  parent_agent_id?: string | null;
}

export interface IfNode extends _BaseNode {
  kind: "if";
  expression: string;
}

export interface DataStoreNode extends _BaseNode {
  kind: "data_store";
  op: "write" | "read" | "query";
  table: string;
  key: string;
  payload: Record<string, unknown>;
  filter: Record<string, unknown>;
  parent_agent_id?: string | null;
  tool_description?: string;
}

export type WorkflowNode =
  | AgentNode
  | ActionNode
  | ConditionNode
  | IfNode
  | ForEachNode
  | MergeNode
  | WaitForWebhookNode
  | TriggerNode
  | DataStoreNode
  | MemoryNode
  | OutputParserNode;

export interface WorkflowDefinition {
  name: string;
  description: string;
  trigger: string;
  /** Legacy. New definitions should use ``nodes``; both fields may coexist
   *  but ``nodes`` is authoritative when non-empty. */
  agents: AgentDefinition[];
  nodes?: WorkflowNode[];
  human_checkpoints: string[];
  output_format: string;
}

export interface NeedsClarificationResponse {
  status: "needs_clarification";
  session_id: string;
  questions: ClarificationQuestion[];
  round_number: number;
  original_prompt: string;
}

export interface ReadyResponse {
  status: "ready";
  definition: WorkflowDefinition;
  augmented_prompt: string;
  rounds_used: number;
}

export type InterpretResponse = NeedsClarificationResponse | ReadyResponse;

export function isNeedsClarification(
  r: InterpretResponse,
): r is NeedsClarificationResponse {
  return r.status === "needs_clarification";
}

export function isReadyResponse(r: InterpretResponse): r is ReadyResponse {
  return r.status === "ready";
}

export type WorkflowStatus = "draft" | "published" | "archived";

export interface WorkflowSummaryOut {
  id: string;
  workspace_id: string;
  name: string;
  slug: string;
  current_version: number;
  is_active: boolean;
  status: WorkflowStatus;
  published_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface WorkflowCreateBody {
  workspace_id: string;
  definition: WorkflowDefinition;
  slug?: string | null;
  change_note?: string | null;
}

export interface WorkflowVersionOut {
  id: string;
  version: number;
  change_note: string | null;
  created_by: string;
  created_at: string;
  definition: WorkflowDefinition;
}

export interface WorkflowDetailOut {
  workflow: WorkflowSummaryOut;
  versions: WorkflowVersionOut[];
}

export interface WorkflowListOut {
  items: WorkflowSummaryOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface WorkflowUpdateBody {
  definition: WorkflowDefinition;
  change_note?: string | null;
}

/** Body for POST /api/v1/workflows/{id}/augment — NL refinement of an existing graph. */
export interface AugmentRequestPayload {
  message: string;
  current_definition: WorkflowDefinition;
}

/** Response shape from /augment — preview only; never auto-persisted. */
export interface AugmentResponse {
  proposed_definition: WorkflowDefinition;
  changes: string[];
}

export type ExecutionEventType =
  | "workflow_start"
  | "agent_start"
  | "agent_thinking"
  | "tool_call"
  | "tool_result"
  | "agent_complete"
  | "hitl_required"
  | "workflow_complete"
  | "error"
  | "heartbeat"
  | "condition_decided"
  | "if_decided"
  | "for_each_started"
  | "for_each_item"
  | "for_each_complete"
  | "wait_for_webhook"
  | "webhook_resumed"
  | "node_skipped"
  | "trigger_fired"
  | "action_invoked"
  | "action_result"
  | "action_dry_run"
  | "data_store_op"
  | "node_complete";

export interface ExecutionEvent {
  type: ExecutionEventType;
  agent_id?: string | null;
  agent_name?: string | null;
  /** Human-readable SSE payload fragments (varies by event). */
  content?: string | null;
  tool_name?: string | null;
  message?: string | null;
  data?: Record<string, unknown> | null;
  checkpoint_id?: string | null;
  success?: boolean;
  execution_id?: string | null;
  workflow_id?: string | null;
  thread_id?: string | null;
  result?: Record<string, unknown> | null;
  /** node_complete fields — per-node inspection (n8n-style). */
  node_id?: string | null;
  node_name?: string | null;
  node_kind?: string | null;
  action_slug?: string | null;
  input_snapshot?: unknown;
  output_snapshot?: unknown;
  status?: string | null;
  duration_ms?: number | null;
  dry_run?: boolean | null;
  /** workflow_complete carries a readiness verdict + run mode. */
  readiness?: WorkflowReadiness | null;
  /** "live" (published, real actions) or "preview" (draft/test, nothing sent). */
  mode?: "live" | "preview" | null;
  live?: boolean | null;
}

export interface WorkflowReadinessIssue {
  node_id?: string | null;
  node_name?: string | null;
  reason: string;
}

export interface WorkflowReadiness {
  ready: boolean;
  issues: WorkflowReadinessIssue[];
}

export interface ExecutionRequest {
  input_data: Record<string, unknown>;
  variables: Record<string, unknown>;
  /** When true, the backend runs a fully-mocked walk of the graph — no
   *  LLM, no integrations. Mirrors n8n's "Test workflow" button. */
  demo?: boolean;
  /** Only consulted when ``demo=true``. When true AND the workspace has
   *  Azure OpenAI configured, agent nodes call the real LLM (deployment
   *  from ``AZURE_OPENAI_DEPLOYMENT``). Integrations remain dry-run. */
  use_real_llm?: boolean;
  /** Demo only: force condition/if nodes down a chosen branch. */
  branch_overrides?: Record<string, string>;
}

export interface SampleInputResponse {
  input_data: Record<string, unknown>;
}

export interface HITLApprovalBody {
  approved: boolean;
  feedback?: string | null;
}

// ---------------------------------------------------------------------------
// Integrations & tools
// ---------------------------------------------------------------------------

export interface ToolDefinitionOut {
  name: string;
  description: string;
  provider: string;
  parameters: Record<string, unknown>;
}

export interface ToolsListResponse {
  tools: ToolDefinitionOut[];
}

export interface IntegrationResponse {
  id: string;
  provider: string;
  status: string;
  scopes: string[];
  connected_at: string | null;
  last_used: string | null;
  available_tools: string[];
}

// ---------------------------------------------------------------------------
// Native Dynamiq connections (Phase A — no Composio hop)
// ---------------------------------------------------------------------------

export interface NativeProviderField {
  key: string;
  label: string;
  type: "string" | "secret" | "url";
  required: boolean;
  placeholder: string | null;
  help_text: string | null;
}

export interface NativeProviderCatalogEntry {
  id: string;
  name: string;
  category: string;
  description: string;
  auth_type: string;
  icon: string;
  docs_url: string | null;
  tool_slugs: string[];
  fields: NativeProviderField[];
}

export interface NativeProviderCatalogResponse {
  providers: NativeProviderCatalogEntry[];
}

export interface NativeConnectionResponse {
  id: string;
  workspace_id: string;
  provider: string;
  name: string;
  auth_type: string;
  status: string;
  tool_slugs: string[];
  last_test_at: string | null;
  last_test_error: string | null;
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface NativeConnectionCreateRequest {
  provider: string;
  name: string;
  config: Record<string, unknown>;
}

export interface NativeConnectionPatchRequest {
  name?: string;
  config?: Record<string, unknown>;
}

export interface NativeConnectionTestResponse {
  success: boolean;
  message: string;
}

// ---------------------------------------------------------------------------
// MCP servers (Phase C)
// ---------------------------------------------------------------------------

export type MCPTransport = "streamable-http" | "sse";

export interface MCPServerCreateRequest {
  name: string;
  url: string;
  transport: MCPTransport;
  auth_header_name: string;
  auth_header_value: string;
  extra_headers?: Record<string, string>;
}

export interface MCPServerResponse {
  id: string;
  workspace_id: string;
  name: string;
  url: string;
  transport: MCPTransport;
  status: string;
  auth_header_name: string;
  last_test_at: string | null;
  last_test_error: string | null;
  last_tool_count: number | null;
  created_at: string;
  updated_at: string;
}

export interface MCPServerTestResponse {
  success: boolean;
  message: string;
  tool_count: number;
  sample_tool_names: string[];
}

// ---------------------------------------------------------------------------
// Documents & RAG
// ---------------------------------------------------------------------------

export interface DocumentSummaryOut {
  id: string;
  filename: string;
  file_type: string;
  status: string;
  chunk_count: number;
  page_count: number;
  created_at: string;
}

export interface DocumentUploadResponse {
  document_id: string;
  status: string;
  deduplicated?: boolean;
}

export interface DocumentListResponse {
  items: DocumentSummaryOut[];
  total: number;
  page: number;
  page_size: number;
}

export interface DocumentQueryBody {
  question: string;
  top_k?: number;
}

export interface CitationOut {
  index: number;
  document_title: string;
  page_number: number;
  chunk_index: number;
  document_id: string;
}

export interface CitedAnswerOut {
  answer: string;
  citations: CitationOut[];
  confidence: number;
  unanswerable: boolean;
}

export interface DocumentStatusOut {
  document_id: string;
  status: string;
  chunk_count: number;
  error_message: string | null;
  indexed_at: string | null;
}

// ---------------------------------------------------------------------------
// Dialog / Chat
// ---------------------------------------------------------------------------

export interface DialogTurnBody {
  message: string;
  workspace_id: string;
}

// ---------------------------------------------------------------------------
// Dashboard aggregates (lightweight composites)
// ---------------------------------------------------------------------------

export interface ExecutionListItemDto {
  id: string;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  duration_ms: number | null;
  error_message: string | null;
}

export interface WorkflowExecutionsEnvelope {
  items: ExecutionListItemDto[];
  total: number;
  page: number;
  page_size: number;
}

// ---------------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------------

export interface DailyExecutionCount {
  date: string;
  count: number;
}

export interface OverviewStats {
  total_executions: number;
  successful_executions: number;
  failed_executions: number;
  avg_duration_ms: number | null;
  total_tokens_used: number;
  executions_by_day: DailyExecutionCount[];
}

export interface ConfidenceBucket {
  label: string;
  count: number;
}

export interface RagAnalytics {
  total_queries: number;
  avg_confidence: number | null;
  unanswerable_count: number;
  top_documents: { document_id: string; title: string; query_count: number }[];
  confidence_buckets: ConfidenceBucket[];
}

export interface ToolUsageStat {
  tool_name: string;
  call_count: number;
  success_rate: number;
  avg_duration_ms: number | null;
}

export interface ModelCostRow {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
}

export interface CostStats {
  by_model: ModelCostRow[];
  total_estimated_usd: number;
}
