/**
 * Response types — transcribed from real payloads of the 0.81.2 local API
 * (captured by calling every endpoint against a live server). Fields that the
 * backend may omit are optional here; nothing is typed as a guess.
 */

export type Health = {
  status: string;
  service: string;
  version: string;
  schema_version: number;
  schema_latest: number;
  emergency_stop_engaged: boolean;
  active_profile: string | null;
  running_tasks: string[];
  running_missions: string[];
};

export type Overview = {
  tasks: { total: number; by_status: Record<string, number> };
  events_total: number;
  pending_approvals: number;
  tools: number;
  missions: number;
  schedules: number;
  schedules_enabled: number;
  health: Health;
};

export type SystemSnapshot = {
  captured_at: string;
  platform: { name: string; release: string; machine: string; python: string };
  cpu: { percent: number | null; method: string; logical_cores: number };
  memory: { total_bytes: number; available_bytes: number; used_bytes: number; percent: number; method: string };
  disks: Record<string, { path: string; total_bytes: number; used_bytes: number; free_bytes: number; percent: number }>;
  process: { pid: number; rss_bytes: number; rss_method: string; python_threads: number; uptime_seconds: number };
  network: {
    interfaces: string[];
    bytes_received: number;
    bytes_sent: number;
    receive_bytes_per_second: number | null;
    send_bytes_per_second: number | null;
    method: string;
  };
  gpu: {
    available: boolean;
    name: string | null;
    utilization_percent: number | null;
    memory_used_mib: number | null;
    memory_total_mib: number | null;
    method: string;
  };
  top_processes?: { pid: number; name: string; rss_bytes: number }[];
};

export type SystemPayload = {
  snapshot: SystemSnapshot;
  history: {
    captured_at: string;
    cpu_percent: number | null;
    memory_percent: number | null;
    receive_bytes_per_second: number | null;
    send_bytes_per_second: number | null;
  }[];
};

export type AppEvent = {
  id: number;
  event_type: string;
  source: string;
  task_id: string | null;
  summary: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type EventsPayload = { events: AppEvent[]; cursor?: number; total: number };

export type Task = {
  id: string;
  instruction: string;
  status: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error: string | null;
  result?: string | null;
  mission_id?: string | null;
};

export type TasksPayload = { tasks: Task[]; total: number; limit: number; offset: number };

export type Mission = {
  id: string;
  name: string;
  description: string;
  status: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  progress: { total: number } & Record<string, number>;
};

export type MissionsPayload = { missions: Mission[]; total: number };

export type Approval = {
  key: string;
  tool?: string;
  tool_id?: string;
  status?: string;
  summary?: string;
  scope?: Record<string, unknown>;
  requested_at?: string;
  expires_at?: string | null;
  task_id?: string | null;
  [extra: string]: unknown;
};

export type ApprovalsPayload = { requests: Approval[]; pending_count: number; total?: number };

export type Agent = {
  name: string;
  role: string;
  description: string;
  system_prompt: string;
  allowed_tools: string[];
  provider: string;
  max_steps: number;
  token_budget: number | null;
  builtin: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
};

export type AgentsPayload = { agents: Agent[] };
export type AgentStatsPayload = {
  agents: { agents: number; agents_enabled: number; agent_runs: number; builtin_agents: number };
};
export type AgentRunsPayload = {
  runs: {
    run_id?: string;
    agent?: string;
    status?: string;
    started_at?: string;
    finished_at?: string | null;
    steps?: number;
    [extra: string]: unknown;
  }[];
};

export type ProvidersPayload = {
  order: string[];
  strategy: string;
  route: string[];
  providers: { name?: string; slot?: string; available?: boolean; models?: string[]; [extra: string]: unknown }[];
};

export type UsagePayload = {
  analytics_enabled: boolean;
  usage: {
    calls: number;
    successes: number;
    success_rate: number | null;
    input_tokens: number;
    output_tokens: number;
    estimated_cost: number | null;
    by_model: { model?: string; calls?: number; tokens?: number; [extra: string]: unknown }[];
  };
};

export type SafetyPayload = {
  hard_timeouts: boolean;
  worker_isolation: boolean;
  max_tool_seconds: number;
  approval_expiry_minutes: number;
  emergency_stop: { engaged: boolean; source: string; reason: string; engaged_at: string | null; engaged_by: string | null };
  emergency_stop_file: string;
  pending_approvals: number;
};

export type VoicePayload = {
  available: boolean;
  microphone: {
    enabled: boolean;
    state: string;
    mode: string;
    listening: boolean;
    wake_word_enabled: boolean;
    wake_word: string;
    wake_word_available: boolean;
    indicator: string;
    retention: string;
    store_transcripts: boolean;
    cloud_stt_allowed: boolean;
    max_seconds: number;
    backend: string;
    backend_reason: string;
    last_reason: string;
  };
  stt: { engine: string; label: string; local: boolean; available: boolean; reason: string; engines: VoiceEngine[] };
  tts: { engine: string; label: string; available: boolean; reason: string; engines: VoiceEngine[] };
  input_devices: { available: boolean; method: string; reason: string; count: number; devices: string[] };
  output_devices: { available: boolean; method: string; reason: string; count: number; devices: string[] };
  sessions: { total: number; audio_seconds: number; transcript_chars: number; retained: number; last_at: string | null };
  emergency_stop: boolean;
  work_dir: string;
};

export type VoiceEngine = { id: string; kind: string; label: string; local: boolean; available: boolean; reason: string };

export type KnowledgeStatsPayload = {
  knowledge: {
    documents: number;
    chunks: number;
    embedding_models: string[];
    active_embedding_model: string;
    embedding_local: boolean;
    memories: { note: number; workflow: number };
    embedded_notes: number;
    supported_suffixes: string[];
  };
};

export type KnowledgeDocumentsPayload = {
  documents: {
    id: number;
    source_name: string;
    kind: string;
    title: string;
    sha256: string;
    characters: number;
    chunk_count: number;
    embedding_model: string;
    created_at: string;
  }[];
  total: number;
};

export type MemorySearchPayload = {
  query: string;
  mode: string;
  embedding_model: string;
  searched_chunks: number;
  results: { document_id?: number; source_name?: string; chunk_index?: number; score?: number; text?: string; [extra: string]: unknown }[];
};

export type ToolsPayload = {
  tools: {
    id: string;
    name: string;
    version: string;
    description: string;
    permissions: string[];
    timeout_seconds: number;
    network_required: boolean;
    provenance: string;
  }[];
};

export type FlowsPayload = {
  flows: { id: string; name: string; status?: string; enabled?: boolean; [extra: string]: unknown }[];
  total: number;
  summary: {
    flows: number;
    total: number;
    triggers_enabled: number;
    runs: number;
    by_status: Record<string, number>;
    last_run_at: string | null;
  };
};

export type FlowsSummary = {
  flows: number;
  total: number;
  triggers_enabled: number;
  runs: number;
  by_status: Record<string, number>;
  last_run_at: string | null;
};

/** `/v1/flows/stats` wraps the summary under a `flows` key (unlike the list route). */
export type FlowsStatsPayload = { flows: FlowsSummary };

export type SchedulesPayload = {
  schedules: {
    id: string;
    name: string;
    instruction: string;
    kind: string;
    run_at: string | null;
    interval_seconds: number | null;
    next_run_at: string | null;
    enabled: boolean;
    use_ai: boolean;
    created_at: string;
    updated_at: string;
    last_run_at: string | null;
    last_task_id: string | null;
    run_count: number;
  }[];
  total: number;
  scheduler_interval_seconds: number;
};

export type RemindersPayload = {
  reminders: {
    id: string;
    title: string;
    note: string;
    due_at: string;
    repeat_seconds: number | null;
    severity: string;
    enabled: boolean;
    status: string;
    source: string;
    created_at: string;
    updated_at: string;
    last_fired_at: string | null;
    fired_count: number;
  }[];
  total: number;
};

export type NotificationsPayload = {
  notifications: {
    id: string;
    kind: string;
    severity: string;
    title: string;
    body: string;
    channels: string;
    status: string;
    detail: string;
    reminder_id: string | null;
    dedupe_key: string;
    created_at: string;
  }[];
  total: number;
};

export type ProactiveStatus = {
  enabled: boolean;
  outbound_enabled: boolean;
  quiet_hours: string;
  quiet_now: boolean;
  briefing_enabled: boolean;
  briefing_time: string;
  notifications_last_hour: number;
  notification_max_per_hour: number;
  cooldown_seconds: number;
  now: string;
  reminders_open: number;
  reminders_total: number;
  notifications: number;
  briefings: number;
  last_notification_at: string | null;
  last_briefing_at: string | null;
  next_due_at: string | null;
};

export type SettingsPayload = { settings: Record<string, string> };

export type PlanPayload = { settings: Record<string, string> };

export type I18nStatus = {
  locale: string;
  name: string;
  direction: string;
  rtl: boolean;
  coverage: { locale: string; total_keys: number; translated_keys: number; missing_keys: string[]; percent: number; direction: string };
  notice: string;
  locales: { locale: string; name: string; direction: string; is_default: boolean; total_keys: number; translated_keys: number; percent: number }[];
};

export type A11yStatus = {
  locale: string;
  language_name: string;
  direction: string;
  rtl: boolean;
  scale: number;
  theme: string;
  high_contrast: boolean;
  reduce_motion: boolean;
  keyboard_shortcuts: boolean;
  cli_color: string;
  supported_locales: string[];
  coverage: I18nStatus["coverage"];
  locales: I18nStatus["locales"];
};

export type PerfBench = {
  iterations: number;
  python: string;
  platform: string;
  measurements_ms: Record<string, number>;
  schema: number;
  notes: string;
};

export type SyncStatus = {
  enabled: boolean;
  gate: string;
  device: { device_id: string; fingerprint: string; sign_public: string; encrypt_public: string };
  device_name: string;
  transport: string;
  sync_dir: string;
  sync_dir_state: string;
  conflict_policy: string;
  limits: Record<string, number>;
  collections: string[];
  counts: { peers: number; bundles: number; pending_requests: number };
  peers: unknown[];
  last_export_at: string;
  last_import_at: string;
  detail: string;
};

export type PluginsStatus = {
  enabled: boolean;
  gate: string;
  signature_required: boolean;
  unsigned_allowed: boolean;
  trusted_keys: string[];
  capabilities: string[];
  denied_capabilities: string[];
  sandbox: { platform: string; mechanism: string; os_limits: string[]; timeout: string };
  limits: Record<string, string | number | boolean>;
  counts: { plugins: number; enabled: number; runs: number };
  plugins: unknown[];
  detail: string;
};

export type ConnectorsStatus = {
  allow_connectors: boolean;
  allow_email_connector: boolean;
  max_items: number;
  timeout_seconds: number;
  connectors: {
    connector: string;
    enabled: boolean;
    configured: boolean;
    reason: string;
    target: string;
    [extra: string]: unknown;
  }[];
  enabled: string[];
  ledger: { events: number; deliveries: number } & Record<string, unknown>;
};

export type AutomationStats = { automation: { total: number; by_kind: Record<string, number>; refused: number; last_at: string | null } };

export type ProfilesPayload = {
  profiles: { name: string; [extra: string]: unknown }[];
  active?: string | null;
  total?: number;
};

export type RunPayload = { task?: Task; [extra: string]: unknown };
