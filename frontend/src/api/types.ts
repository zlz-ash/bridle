// Mirror of backend Pydantic schemas.

export type NodeStatus =
  | 'pending'
  | 'ready'
  | 'running'
  | 'blocked'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'archived'
  | 'proposed'
  | 'ratified'
  | 'mapping'
  | 'executing'
  | 'verifying'
  | 'drifted';

export interface NodeTestDetail {
  command: string;
  purpose: string;
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
  test_details: NodeTestDetail[];
  test_count: number;
  metrics: Record<string, unknown>;
  constraints: Record<string, unknown>;
  review_checks: string[];
  expected_outputs: Record<string, unknown>;
  interfaces: Record<string, unknown>;
  read_set: string[];
  write_set: string[];
  readonly_context: string[];
  conflict_contributions: unknown[];
  status: NodeStatus;
  created_at: string;
  updated_at: string;
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

export interface ProjectRead {
  id: string;
  path: string;
  name: string;
  available: boolean;
  scan_status: string;
  can_chat: boolean;
  can_edit_plan: boolean;
  readiness_reason: string | null;
  last_opened_at: string;
}

export interface ProjectSession {
  id: string;
  project_id: string;
  project_path: string;
  title: string;
  role: 'planning' | 'executing';
  status: string;
  available: boolean;
  readonly_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlanMapNode {
  id: string;
  parent_id: string | null;
  order: number;
  status: NodeStatus;
  node_type: string;
  title: string;
  goal: string;
  depends_on: string[];
  [key: string]: unknown;
}

export interface PlanMapOverview {
  project_id: string;
  scan_status: string;
  can_chat: boolean;
  can_edit_plan: boolean;
  readiness_reason: string | null;
  plan_node_count: number;
  code_entity_count: number;
  roots: PlanMapNode[];
  change_seq: number;
}

export interface PlanMapPage {
  items: PlanMapNode[];
  next_cursor: string | null;
}

export interface PlanMapChanges {
  items: Array<{
    change_seq: number;
    entity_type: string;
    entity_id: string;
    operation: string;
    payload: Record<string, unknown>;
    created_at: string;
  }>;
  last_seq: number;
}

export interface CodeEntity {
  id: string;
  path: string;
  kind: string;
  name: string;
  parent_id: string | null;
  payload: Record<string, unknown>;
}

export interface CodeEntityPage {
  items: CodeEntity[];
  next_cursor: string | null;
  has_more?: boolean;
}

export interface PathSlice {
  path: string;
  entities: CodeEntity[];
  relations: CodeRelation[];
  blind_spots: BlindSpot[];
}

export interface CodeRelation {
  source_id: string;
  target_id: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface CodeRelationPage {
  items: CodeRelation[];
  next_cursor: string | null;
  has_more?: boolean;
}

export interface SemanticAnnotation {
  id: string;
  source_id: string;
  summary: string;
  evidence: Record<string, unknown>;
  model: string;
  confidence: number;
  file_hash: string;
  status: 'active' | 'pending' | 'rejected' | 'stale';
}

export interface SemanticAnnotationPage {
  items: SemanticAnnotation[];
  next_cursor: string | null;
  has_more?: boolean;
}

export interface BlindSpot {
  id: string;
  kind: string;
  file_path: string | null;
  range: Record<string, unknown>;
  detail: Record<string, unknown>;
  source: string;
  status: string;
}

export interface BlindSpotPage {
  items: BlindSpot[];
  truncated: boolean;
}

export interface BoundaryConflict {
  path_a: string;
  path_b: string;
  module_a: string;
  module_b: string;
  weight: number;
  reason: string;
}

export interface BoundaryOverview {
  items: BoundaryConflict[];
  debt_nodes: string[];
}

export interface ModuleCandidateFile {
  file_path: string;
  role: string;
  file_hash: string;
  evidence: Record<string, unknown>;
}

export interface ModuleCandidate {
  id: string;
  run_id: string;
  module_id: string;
  name: string;
  status: 'candidate' | 'confirmed' | 'rejected' | 'stale';
  confidence: number;
  evidence_id: string;
  metrics: Record<string, unknown>;
  file_fingerprint: string;
  is_execution_boundary: boolean;
  created_at: string;
  confirmed_at: string | null;
  files?: ModuleCandidateFile[];
}

export interface ModuleCandidatePage {
  items: ModuleCandidate[];
}

export interface ModuleInterfaceCandidate {
  id: string;
  run_id: string;
  from_module: string;
  to_module: string;
  from_candidate_id: string;
  to_candidate_id: string;
  symbol: string;
  signature: Record<string, unknown>;
  evidence: Record<string, unknown>;
  mock_file_path: string;
  mock_hash: string;
  confidence: number;
  status: 'candidate' | 'confirmed' | 'rejected' | 'stale';
  created_at: string;
  confirmed_at: string | null;
}

export interface ModuleInterfaceCandidatePage {
  items: ModuleInterfaceCandidate[];
}

export interface InterfaceMockArtifact {
  id: string;
  interface_candidate_id: string;
  file_path: string;
  file_hash: string;
  status: 'generated' | 'confirmed' | 'rejected' | 'stale';
  payload: Record<string, unknown>;
  created_at: string;
}

export interface InterfaceMockArtifactPage {
  items: InterfaceMockArtifact[];
}

export interface MapArbitrationPage {
  items: Array<{
    id: string;
    objection_type: string;
    status: string;
    related_node_ids: string[];
  }>;
}
