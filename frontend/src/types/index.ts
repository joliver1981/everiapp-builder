// User & Auth types
export type Role = 'admin' | 'developer' | 'user'

export interface User {
  id: string
  username: string
  display_name: string
  email: string
  role: Role
  groups: string[]
  created_at: string
}

export interface AuthTokens {
  access_token: string
  token_type: string
}

// App types
export interface App {
  id: string
  name: string
  description: string
  icon: string
  status: 'draft' | 'published' | 'archived'
  current_version: number
  ai_toggle_enabled: boolean
  bug_widget_enabled?: boolean
  bug_fix_auto_approve_max_risk?: 'none' | 'low' | 'medium'
  ai_verify_level?: 'off' | 'tsc' | 'tsc_build' | 'tsc_build_boot' | 'tsc_build_boot_runtime' | 'tsc_build_boot_runtime_a11y'
  ai_verify_max_iterations?: number
  setup_instructions?: string
  last_published_version?: string
  marketplace_listing?: {
    short_description?: string
    category?: string
    tags?: string[]
    license?: string
  } | null
  created_by: string
  created_at: string
  updated_at: string
}

export interface AppVersion {
  id: string
  app_id: string
  version: number
  published_by: string
  notes: string
  created_at: string
}

// Chat types
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  files_changed?: FileChange[]
  // Code locations the AI pointed at this turn (from [[jump:...]] directives) — rendered
  // as clickable "jump to code" chips under the message.
  codeRefs?: CodeRef[]
  timestamp: string
}

export interface FileChange {
  path: string
  action: 'create' | 'modify' | 'delete'
  content?: string
}

// A pointer to a file + optional line range, used to jump+highlight in the Code panel.
export interface CodeRef {
  path: string
  start: number | null
  end: number | null
}

// What the user is currently looking at in the Code panel, sent to the AI from the in-code
// collaboration overlay so it can focus on the exact code on screen / the highlighted selection.
export interface EditorContext {
  path: string
  selectionText: string | null
  selStartLine: number | null
  selEndLine: number | null
  viewportStartLine: number | null
  viewportEndLine: number | null
}

// Secret types
export interface Secret {
  id: string
  name: string
  category: string
  description: string
  is_set: boolean
  created_at: string
  updated_at: string
}

export interface AppSetting {
  key: string
  label: string
  type: 'string' | 'secret' | 'number' | 'boolean' | 'select' | 'url'
  description: string
  required: boolean
  default_value?: string
  global_secret_ref?: string
  value?: string
}

// Deployment types
export type DeployerKind = 'agent' | 'ssh'

export interface DeploymentTarget {
  id: string
  name: string
  kind: DeployerKind
  host: string
  port: number
  ssh_user: string | null
  port_range_start: number
  port_range_end: number
  environment: string
  credential_secret_id: string | null
  extra_config: Record<string, unknown>
  is_active: boolean
  last_seen_at: string | null
  last_seen_status: string | null
  agent_version: string | null
  created_at: string
  updated_at: string
}

export interface Deployment {
  id: string
  app_id: string
  version: number
  target_id: string
  allocated_port: number | null
  status: 'pending' | 'building' | 'uploading' | 'running' | 'stopped' | 'failed'
  public_url: string | null
  deployed_by: string
  started_at: string
  stopped_at: string | null
  last_health_at: string | null
  last_health_status: string | null
  error: string | null
}

export interface TargetTestResult {
  ok: boolean
  detail: string
  agent_version: string | null
  ports_used: number[]
  ports_total: number | null
}

// Bug report types
export type BugRiskLevel = 'low' | 'medium' | 'high'
export type BugReportStatus =
  | 'new' | 'analyzing' | 'analyzed' | 'approved'
  | 'applying' | 'testing' | 'deploying' | 'resolved'
  | 'rejected' | 'failed'

export interface ProposedFile {
  path: string
  action: 'create' | 'update' | 'delete'
  content: string
  current_content?: string | null
}

export interface BugAnalysis {
  id: string
  bug_report_id: string
  diagnosis: string
  root_cause: string
  proposed_files: ProposedFile[]
  risk_level: BugRiskLevel
  risk_rationale: string
  llm_model: string | null
  created_at: string
}

export interface FixAttempt {
  id: string
  bug_report_id: string
  analysis_id: string
  base_version: number | null
  new_version: number | null
  deployment_id: string | null
  status: string
  auto_approved: boolean
  approved_by: string | null
  approved_at: string | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface BugReportSummary {
  id: string
  app_id: string
  app_name: string | null
  version: number | null
  title: string
  status: BugReportStatus
  risk_level: BugRiskLevel | null
  auto_approve_enabled: boolean
  reporter_label: string | null
  created_at: string
}

export interface BugReportDetail {
  id: string
  app_id: string
  version: number | null
  deployment_id: string | null
  reporter_user_id: string | null
  reporter_label: string | null
  title: string
  description: string
  captured_context: {
    page_url?: string
    user_agent?: string
    viewport?: { width: number; height: number }
    console_tail?: string[]
    network_errors?: Array<{ url: string; status: number | null; method: string; error: string | null }>
    extra?: Record<string, unknown>
  }
  screenshot_url: string | null
  status: BugReportStatus
  error: string | null
  created_at: string
  updated_at: string
  analyses: BugAnalysis[]
  attempts: FixAttempt[]
}

// AI Provider types
export interface AIProvider {
  id: string
  name: string
  provider_type: string
  base_url: string
  is_active: boolean
  is_default_generation: boolean
  is_default_toggle: boolean
  default_model: string
  last_verified: string | null
  created_at: string
}

// --- Data platform: Connections + Datasets --------------------------------

export type ConnectionKind = 'sql' | 'rest'

export interface Connection {
  id: string
  name: string
  description: string
  kind: ConnectionKind
  config: Record<string, unknown>
  credential_secret_ref: string | null
  default_row_limit: number
  default_timeout_seconds: number
  read_only: boolean
  created_by: string
  created_at: string
  updated_at: string
}

export interface ConnectionTestResult {
  success: boolean
  message: string
  response_time_ms: number | null
}

export type DatasetKind = 'table' | 'query' | 'api_call'
export type DatasetVisibility = 'private' | 'app_scoped' | 'org'

export interface Dataset {
  id: string
  name: string
  description: string
  connection_id: string
  kind: DatasetKind
  definition: Record<string, unknown>
  parameter_schema: Record<string, unknown>
  output_schema: Record<string, unknown>
  row_limit_override: number | null
  timeout_override: number | null
  visibility: DatasetVisibility
  owner_id: string
  created_at: string
  updated_at: string
}

export interface DatasetPreviewColumn {
  name: string
  type: string
}

export interface DatasetPreviewResult {
  rows: Array<Record<string, unknown>>
  columns: DatasetPreviewColumn[]
  row_count: number
  truncated: boolean
  duration_ms: number
}

export interface SchemaIntrospectionResult {
  schemas: string[]
  tables: string[]
  columns: Array<{ name: string; type: string; nullable: boolean }>
}

export interface DatasetRecentCall {
  action: string
  user_id: string
  details: string
  created_at: string
}

export interface DatasetRecentCallsResult {
  calls: DatasetRecentCall[]
}
