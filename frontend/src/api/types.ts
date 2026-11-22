export type AgentId =
  | 'antigravity'
  | 'claude_code'
  | 'codex'
  | 'cursor'
  | 'gemini_cli'
  | 'windsurf'
  | 'cline'
  | 'roo_code'
  | 'copilot'
  | 'opencode'
  | 'aider'
  | (string & {});

export type SearchMode = 'hybrid' | 'fts' | 'vector';
export type TurnRole = 'user' | 'assistant' | 'system' | 'tool';
export type ToolCallStatus = 'ok' | 'error' | 'pending';
export type MemoryCategory = 'preference' | 'decision' | 'pitfall' | 'fact';
export type DistillStatus = 'pending' | 'heuristic' | 'llm_distilled';
export type MemoryOrigin = 'manual' | 'distilled' | 'native_import';
export type AdapterHealthStatus = 'healthy' | 'degraded' | 'not_found' | 'permission_denied';
export type DistillerProvider = 'heuristic' | 'ollama' | 'openai_compatible';

export interface ToolCallRecord {
  id: string;
  name: string;
  arguments: Record<string, unknown> | string;
  output?: string | null;
  status: ToolCallStatus;
  duration_ms?: number | null;
}

export interface NormalizedTurn {
  id: string;
  session_id: string;
  turn_index: number;
  role: TurnRole;
  content: string;
  thinking?: string | null;
  tool_calls: ToolCallRecord[];
  timestamp?: string | null;
  model?: string | null;
  token_count?: number | null;
  redacted: boolean;
}

export interface NormalizedSession {
  id: string;
  agent: AgentId;
  external_id: string;
  title: string;
  workspace?: string | null;
  source_path: string;
  started_at?: string | null;
  updated_at?: string | null;
  turn_count: number;
  model?: string | null;
  tags: string[];
  summary?: string | null;
  distill_status?: DistillStatus;
  turns?: NormalizedTurn[];
}

export interface MemoryCard {
  id: string;
  title: string;
  category: MemoryCategory;
  content: string;
  file_path?: string | null;
  tags: string[];
  workspace?: string | null;
  distill_status: DistillStatus;
  origin: MemoryOrigin;
  reviewed: boolean;
  source_session_id?: string | null;
  source_agent?: AgentId | null;
  source_turn_index?: number | null;
  native_source_path?: string | null;
  confidence?: number | null;
  created_at: string;
  updated_at: string;
}

export interface SearchHit {
  turn_id: string;
  session_id: string;
  agent: AgentId;
  session_title: string;
  workspace?: string | null;
  turn_index: number;
  role: TurnRole;
  snippet: string;
  content: string;
  timestamp?: string | null;
  score: number;
  fts_score?: number | null;
  vector_score?: number | null;
  match_mode: SearchMode;
}

export interface SearchResponse {
  query: string;
  mode: SearchMode;
  total: number;
  took_ms: number;
  hits: SearchHit[];
  facets: {
    agents: Record<string, number>;
    workspaces: Record<string, number>;
  };
}

export interface SessionsResponse {
  total: number;
  limit: number;
  offset: number;
  sessions: NormalizedSession[];
}

export interface SessionDetailResponse {
  session: NormalizedSession;
  linked_memories: MemoryCard[];
  total_turns?: number;
  turn_offset?: number;
  turn_limit?: number;
  has_earlier_turns?: boolean;
  has_later_turns?: boolean;
  truncated_turn_indices?: number[];
}

export interface SessionTurnResponse {
  turn: NormalizedTurn;
  truncated: boolean;
}

export interface NativeMemoryImportStatus {
  agent: AgentId;
  source_dir: string;
  discovered_files: number;
  imported_count: number;
  last_imported_at?: string | null;
  status: 'synced' | 'pending' | 'not_found' | 'error';
}

export interface MemoriesResponse {
  total: number;
  counts_by_category: Record<MemoryCategory, number>;
  counts_by_status: Record<DistillStatus | 'manual', number>;
  native_imports: NativeMemoryImportStatus[];
  memories: MemoryCard[];
}

export interface CreateMemoryPayload {
  title: string;
  category: MemoryCategory;
  content: string;
  tags?: string[];
  workspace?: string | null;
  source_session_id?: string | null;
  source_agent?: AgentId | null;
  reviewed?: boolean;
}

export interface UpdateMemoryPayload {
  title?: string;
  category?: MemoryCategory;
  content?: string;
  tags?: string[];
  workspace?: string | null;
  reviewed?: boolean;
  distill_status?: DistillStatus;
}

export interface SyncMemoriesPayload {
  import_native?: boolean;
  overwrite_existing?: boolean;
}

export interface SyncMemoriesResponse {
  synced_files: number;
  added: number;
  updated: number;
  deleted: number;
  imported_native: number;
  native_imports: NativeMemoryImportStatus[];
}

export interface HandoffBriefSections {
  goal: string;
  current_state: string;
  key_decisions: string[];
  modified_files: string[];
  pitfalls_and_constraints: string[];
  next_steps: string[];
  relevant_memories: Array<{
    id: string;
    title: string;
    category: MemoryCategory;
  }>;
}

export interface HandoffBrief {
  session_id: string;
  source_agent: AgentId;
  target_agent: AgentId;
  workspace?: string | null;
  title: string;
  markdown: string;
  sections: HandoffBriefSections;
  token_estimate: number;
  generated_at: string;
  redacted: boolean;
}

export interface HandoffRequest {
  session_id: string;
  target_agent: AgentId;
  include_memories?: boolean;
  include_tool_summary?: boolean;
  max_tokens?: number;
  custom_instructions?: string;
}

export interface AdapterHealth {
  id: AgentId;
  name: string;
  status: AdapterHealthStatus;
  source_paths: string[];
  discovered_files: number;
  session_count: number;
  turn_count: number;
  native_memory_count: number;
  last_scan_at?: string | null;
  last_error?: string | null;
  watcher_active: boolean;
  supports_native_memories: boolean;
}

export interface AdaptersResponse {
  total_discovered_sources: number;
  watcher_running: boolean;
  adapters: AdapterHealth[];
}

export interface ScanRequest {
  agents?: AgentId[];
  full_rescan?: boolean;
  distill?: boolean;
  defer_embeddings?: boolean;
}

export interface ScanResponse {
  job_id: string;
  status: 'started' | 'completed';
  scanned_files: number;
  new_sessions: number;
  new_turns: number;
  updated_sessions: number;
  errors: Array<{ agent: string; path: string; error: string }>;
  duration_ms: number;
}

export interface DistillerSettings {
  provider: DistillerProvider;
  model?: string | null;
  base_url?: string | null;
  api_key_configured: boolean;
  auto_distill_on_scan: boolean;
  zero_cost_mode: boolean;
  pending_sessions_count: number;
  distilled_memories_count: number;
}

export interface UpdateDistillerSettingsPayload {
  provider?: DistillerProvider;
  model?: string | null;
  base_url?: string | null;
  api_key?: string | null;
  auto_distill_on_scan?: boolean;
}

export interface DistillBackfillRequest {
  limit?: number;
  agent?: AgentId | null;
  force?: boolean;
}

export interface DistillBackfillResponse {
  job_id: string;
  status: 'started' | 'completed';
  provider: DistillerProvider;
  processed_sessions: number;
  created_memories: number;
  skipped_sessions: number;
}

export interface AgentStatItem {
  agent: AgentId;
  name: string;
  sessions: number;
  turns: number;
  native_memories: number;
  last_activity_at?: string | null;
}

export interface StatsResponse {
  total_sessions: number;
  total_turns: number;
  total_memories: number;
  total_native_memories: number;
  db_size_bytes: number;
  index_embeddings_count: number;
  by_agent: AgentStatItem[];
  memories_by_category: Record<MemoryCategory, number>;
  memories_by_status: Record<DistillStatus | 'manual', number>;
  recent_activity: Array<{
    date: string;
    sessions: number;
    turns: number;
  }>;
  distiller: DistillerSettings;
}

export interface HealthResponse {
  status: 'ok' | 'degraded';
  version: string;
  db_ready: boolean;
  distiller_provider: DistillerProvider;
}

export interface OmnimexEvent {
  type:
    | 'connected'
    | 'scan.started'
    | 'scan.progress'
    | 'scan.completed'
    | 'session.ingested'
    | 'memory.created'
    | 'memory.updated'
    | 'memory.synced'
    | 'distill.progress'
    | 'distill.completed'
    | 'embedding.started'
    | 'embedding.progress'
    | 'embedding.completed'
    | 'heartbeat';
  data: Record<string, unknown>;
  receivedAt: string;
}
