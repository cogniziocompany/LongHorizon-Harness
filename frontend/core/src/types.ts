export type RunStatus = 'idle' | 'running' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled' | string;

/** Must stay aligned with the server-side ingress ceiling. */
export const MAX_ROUNDS = 1000;

export interface EventEnvelope {
  schema_version: number;
  event_id: string;
  type: string;
  ts: number;
  run_id: string;
  round: number | null;
  role: string | null;
  status: string | null;
  payload: Record<string, unknown>;
  legacy: Record<string, unknown>;
  offset?: number;
}

export interface ApprovalOption {
  value: string;
  label: string;
  style?: string;
}

export interface Approval {
  approval_id: string;
  title: string;
  message: string;
  options: ApprovalOption[];
  answers: string[];
  allow_input: boolean;
  input_label: string;
  /** Budget gates let the operator grant a specific number of extra rounds. */
  allow_extra_rounds?: boolean;
  context: Record<string, unknown>;
  round_index: number;
  status: 'pending' | 'resolved' | string;
  action: string;
  reason: string;
  user_input: string;
  extra_rounds?: number;
  created_at: number;
  resolved_at: number | null;
}

export interface OperatorMessage {
  id: string;
  text: string;
  created_at: number;
  status: 'queued' | 'applied' | 'rejected' | 'failed' | 'cancelled' | string;
}

export interface RoundView {
  round_index: number;
  next_step?: string;
  plan_text?: string;
  task_state?: string;
  task_contract?: string;
  executor_output?: string;
  auditor_report?: string;
  harness_feedback?: string;
  /** User-facing closing reply written after the terminal audit decision. */
  final_response?: string;
  active_role?: string | null;
  in_progress?: boolean;
  roles?: string[];
  role_sizes?: Record<string, number>;
  manager_status?: Record<string, unknown>;
  executor_status?: Record<string, unknown>;
  auditor_status?: Record<string, unknown>;
  final_response_status?: Record<string, unknown>;
}

export type ContentionTier = 'same_repo' | 'same_repo_same_branch' | 'shared_git_dir' | 'same_tree';

export interface ContentionPeer {
  run_id: string;
  workspace: string;
  branch?: string | null;
}

export interface WorkspaceContention {
  contention_id: string;
  severity: ContentionTier;
  peers: ContentionPeer[];
}

export interface RunSummary {
  id: string;
  task: string;
  status: RunStatus;
  updated_at: number;
  log_dir: string;
  agent?: string;
  model?: string | null;
  role_configs?: Record<'manager' | 'executor' | 'auditor', { agent: string; model: string }>;
  workspace?: string;
  max_rounds?: number;
  prompt_language?: 'en' | 'zh';
  contention?: WorkspaceContention;
}

export interface Snapshot {
  schema_version: number;
  run: {
    id: string;
    status: RunStatus;
    started_at?: number | null;
    finished_at?: number | null;
    log_dir: string;
    /** Optional supervisor result fields (introduced after protocol v1). */
    completion_satisfied?: boolean | null;
    completion_authority?: string | null;
    report_status?: string | null;
    exit_code?: number | null;
    failure_reason?: string | null;
    /** Durable user-facing reply. This is distinct from Auditor protocol text. */
    final_response?: string;
    agent?: string;
    model?: string | null;
    role_configs?: Record<'manager' | 'executor' | 'auditor', { agent: string; model: string }>;
    workspace?: string;
    max_rounds?: number;
    prompt_language?: 'en' | 'zh';
    /** Generation counter: bumped each time a terminal run is resumed in place. */
    resume_epoch?: number;
    /** Lifecycle action the operator asked for: 'stop' or 'abort'. */
    requested_action?: string;
    /** When the stop signal was sent, used to detect a worker ignoring SIGTERM. */
    stop_requested_at?: number;
  };
  mission: { task: string; contract_path: string; plan_path: string; verified_state_path: string; report_path: string };
  rounds: RoundView[];
  active_round: number | null;
  active_role: string | null;
  events: EventEnvelope[];
  approvals: Approval[];
  /** Durable free-form instructions sent by the operator after run creation. */
  operator_messages?: OperatorMessage[];
  controls: { can_inject: boolean; can_abort: boolean; can_resume: boolean };
  diagnostics: {
    last_event_id: string | null;
    event_count: number;
    warnings: string[];
    cursor_gap?: boolean;
    resync_required?: boolean;
  };
  legacy?: Record<string, unknown>;
}

export interface ArtifactList {
  run_id: string;
  round_index: number;
  artifacts: string[];
}

/** One durable task waiting to be launched by the service (Visionary intake). */
export interface QueueEntry {
  queue_id: string;
  name: string;
  task: string;
  workspace: string;
  max_rounds: number;
  trio: string;
  priority: number;
  requested_by: string;
  branch: string;
  continue_branch: boolean;
  base_check: string;
  status: string;
  run_id: string | null;
  reason: string | null;
  skip_reasons: string[];
  created_at: number;
  updated_at: number;
  launched_at: number | null;
  last_checked_at: number | null;
  dedup_key: string | null;
  retry_of: string | null;
  attempt: number;
  failure_cause: string | null;
  /** Visionary intake: the spec file the entry was enqueued from. */
  spec_file: string | null;
  /** Visionary intake: the spec's frontmatter status captured at enqueue time. */
  spec_status: string | null;
  /** Visionary intake: the spec text/inline body itself (the consumed preview). */
  spec: string | null;
  /** Visionary intake: stamped when ``mark_spec_ready`` promotes a spec entry. */
  spec_ready_at: number | null;
}
