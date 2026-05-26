import axios from 'axios'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 10000,
})

// --- Task APIs ---

export interface Task {
  id: string
  title: string
  goal: string | null
  status: string
  created_at: string
  updated_at: string
}

export const taskApi = {
  list: () => api.get<Task[]>('/tasks').then(r => r.data),
  get: (id: string) => api.get<Task>(`/tasks/${id}`).then(r => r.data),
  create: (data: { title: string; goal?: string }) => api.post<Task>('/tasks', data).then(r => r.data),
}

// --- Plan APIs ---

export interface PlanCurrent {
  id: string
  task_id: string
  goal: string
  aggregate_files: AggregateFile[]
  status: string
  created_at: string
  updated_at: string
  nodes: PlanNode[]
}

export interface AggregateFile {
  target_path: string
  contribution_dir: string
  merge_strategy: string
  owner: string
  contributors: string[]
  validation: Record<string, unknown> | unknown[]
}

export interface InterfaceField {
  name: string
  type: string
  required: boolean
  description: string
}

export interface InterfaceEndpoint {
  name: string
  method: string
  path: string
  description: string
}

export interface InterfaceExpose {
  name: string
  fields: InterfaceField[]
  endpoints: InterfaceEndpoint[]
}

export interface InterfaceConsume {
  node_id: string
  interface_name: string
  fields: string[]
  endpoints: string[]
}

export interface NodeInterfaces {
  exposes: InterfaceExpose[]
  consumes: InterfaceConsume[]
}

export interface PlanNode {
  id: string
  plan_id: string
  plan_node_id: string
  title: string
  goal: string
  node_type: string
  order: number
  depends_on: string[]
  files: string[]
  tests: string[]
  metrics: Record<string, unknown> | unknown[]
  constraints: Record<string, unknown> | unknown[]
  review_checks: string[]
  expected_outputs: Record<string, unknown> | unknown[]
  interfaces: NodeInterfaces
  read_set: string[]
  write_set: string[]
  readonly_context: string[]
  conflict_contributions: { aggregate_target: string; contribution_path: string }[]
  container_policy: Record<string, unknown>
  status: string
  created_at: string
  updated_at: string
}

export interface PlanSummary {
  plan_id: string
  goal: string
  task_id: string
  replaced_at: string
  final_status: string
  node_count: number
  completed_count: number
  failed_count: number
  key_nodes: { id: string; title: string; status: string; node_type: string }[]
  key_test_results: { node_id: string; node_title: string; exit_code: number | null; duration_ms: number | null }[]
  key_metrics: Record<string, unknown>
}

export const planApi = {
  current: () => api.get<PlanCurrent>('/plan/current').then(r => r.data),
  importPlan: (taskId: string, data: { goal: string; aggregate_files?: unknown[]; nodes: unknown[] }) =>
    api.post(`/tasks/${taskId}/plan/import`, data).then(r => r.data),
  replacePlan: (data: { goal: string; aggregate_files?: unknown[]; nodes: unknown[] }) =>
    api.put('/plan/current', data).then(r => r.data),
  patchPlan: (data: {
    update_nodes?: unknown[]
    add_nodes?: unknown[]
    remove_node_ids?: string[]
    replace_dependencies?: unknown[]
  }) => api.patch('/plan/current', data).then(r => r.data),
  summary: () => api.get<PlanSummary>('/plan/current/summary').then(r => r.data),
  graph: (taskId: string) => api.get(`/tasks/${taskId}/graph`).then(r => r.data),
}

// --- Node APIs ---

export interface Run {
  id: string
  node_id: string
  status: string
  exit_code: number | null
  started_at: string
  finished_at: string | null
  duration_ms: number | null
  stdout_path: string | null
  stderr_path: string | null
  container_id?: string | null
  container_health?: string | null
  container_error?: string | null
  container_logs?: string[] | null
  container_logs_summary?: string | null
  diagnostic_path?: string | null
  container_status?: string | null
  error_code?: string | null
  test_summary?: string | null
  metrics_summary?: string | null
  integration_result?: Record<string, unknown> | null
  result_summary?: string | null
}

export interface NodeReport {
  node: PlanNode
  runs: Run[]
  evidences: unknown[]
  baseline_run: Run | null
  summary: {
    total_runs: number
    completed_runs: number
    failed_runs: number
    evidence_count: number
    missing_evidence_count: number
  }
}

export const nodeApi = {
  get: (id: string) => api.get<PlanNode>(`/nodes/${id}`).then(r => r.data),
  run: (id: string) => api.post<{ run_id: string; node_id: string; status: string }>(`/nodes/${id}/run`).then(r => r.data),
  runs: (id: string) => api.get<Run[]>(`/nodes/${id}/runs`).then(r => r.data),
  report: (id: string) => api.get<NodeReport>(`/nodes/${id}/report`).then(r => r.data),
}

// --- Agent Proposal APIs ---

export interface FilePatch {
  path: string
  change_type: string
  diff: string
}

export interface AgentProposal {
  summary: string
  file_patches: FilePatch[]
  tests_to_run: string[]
}

export interface ProposalRecord {
  id: string
  node_id: string
  plan_node_id: string
  status: string
  instruction: string
  allowed_files: string[]
  accessible_context: Record<string, unknown>
  proposal: AgentProposal
  source: string
  created_at: string
  updated_at: string
}

export const proposalApi = {
  create: (nodeId: string, data: { instruction: string }) =>
    api.post<ProposalRecord>(`/nodes/${nodeId}/agent/proposals`, data).then(r => r.data),
  list: (nodeId: string) =>
    api.get<ProposalRecord[]>(`/nodes/${nodeId}/agent/proposals`).then(r => r.data),
}

export default api
