// Mirror of backend Pydantic schemas (Frontend-Design.md §3).

export type NodeStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'blocked'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'archived';

export interface Task {
  id: string;
  title: string;
  goal: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface NodeRead {
  id: string;
  plan_id: string;
  plan_node_id: string;
  title: string;
  goal: string;
  node_type: string | null;
  order: number;
  depends_on: string[];
  files: string[];
  tests: string[];
  metrics: Record<string, unknown>;
  constraints: Record<string, unknown>;
  review_checks: string[];
  expected_outputs: Record<string, unknown>;
  interfaces: Record<string, unknown>;
  read_set: string[];
  write_set: string[];
  readonly_context: string[];
  conflict_contributions: unknown[];
  container_policy: Record<string, unknown>;
  status: NodeStatus;
  created_at: string;
  updated_at: string;
}

export interface PlanCurrent {
  id: string;
  task_id: string;
  goal: string;
  status: string;
  created_at: string;
  updated_at: string;
  nodes: NodeRead[];
  aggregate_files?: unknown[];
}

export interface PlanSummary {
  plan_id: string;
  total: number;
  by_status: Record<string, number>;
}

export interface CodingSessionRead {
  session_id: string;
  plan_id: string;
  status: 'creating' | 'active' | 'cancelled' | 'completed' | 'failed';
  mode: string;
  auto_continue_budget: number;
  auto_continue_used: number;
  created_at: string;
  capabilities: Record<string, unknown>;
  main_agent_container: Record<string, unknown> | null;
}

export interface ChatMessage {
  id: string;
  session_id: string;
  role: 'user' | 'assistant' | 'system' | 'tool';
  content: string;
  tool_calls: unknown[] | null;
  tool_result: Record<string, unknown> | null;
  created_at: string;
}

export interface NodeAgentRun {
  run_id: string;
  session_id: string;
  node_id: string;
  plan_node_id: string;
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'timed_out';
  phase: string;
  attempt: number;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  last_heartbeat_at: string | null;
  timeout_at: string | null;
  duration_ms: number | null;
  blocked_reason: string | null;
  result_summary: string;
  container_id: string | null;
  container_status: string | null;
  container_health: string | null;
  container_error: string | null;
  container_logs_summary: string;
  diagnostic_path: string | null;
  error_code: string | null;
  test_summary: string;
  metrics_summary: string;
  integration_result: Record<string, unknown> | null;
  budget_report: Record<string, unknown> | null;
  replan_decision: Record<string, unknown> | null;
}

export interface EligibleNodesResponse {
  session_id: string;
  eligible_nodes: { node_id: string; plan_node_id: string; status: string; title: string }[];
  blocked_nodes: {
    node_id: string;
    plan_node_id: string;
    status: string;
    title: string;
    reason?: string;
    blocked_by?: string[];
  }[];
}

export interface PlanImportPayload {
  goal: string;
  aggregate_files?: unknown[];
  nodes: {
    id: string;
    title: string;
    goal: string;
    depends_on?: string[];
    files?: string[];
    tests?: string[];
    node_type?: string;
    [key: string]: unknown;
  }[];
}

export interface PlanModeConverseResponse {
  reply: string;
  proposed_plan: PlanImportPayload | null;
  parse_error: string | null;
  raw_finish_reason: string | null;
}
