import { apiClient } from './client';
import type {
  ChatMessage,
  ProjectRead,
  ProjectSession,
  PlanMapNode,
  PlanMapOverview,
  PlanMapPage,
  PlanMapChanges,
  CodeEntityPage,
  PathSlice,
  CodeRelationPage,
  SemanticAnnotationPage,
  BlindSpotPage,
  BoundaryOverview,
  MapArbitrationPage,
} from './types';

export type HealthResponse = {
  status: string;
  version: string;
  workspace: string;
  db: string;
  uptime_seconds: number;
  events_subscribers: number;
};

export const healthApi = {
  get: () => apiClient.get<HealthResponse>('/health').then((r) => r.data),
};

/** Project endpoints; path input exits as a persisted project record. */
export const projectsApi = {
  list: () => apiClient.get<{ projects: ProjectRead[] }>('/projects').then((response) => response.data),
  open: (path: string) =>
    apiClient.post<ProjectRead>('/projects/open', { path }).then((response) => response.data),
};

/** Session endpoints; session/message inputs exit as persisted conversation state. */
export const projectSessionsApi = {
  list: (projectId: string) =>
    apiClient.get<ProjectSession[]>('/sessions', { params: { project_id: projectId } })
      .then((response) => response.data),
  create: (projectId: string) =>
    apiClient.post<ProjectSession>('/sessions', { project_id: projectId })
      .then((response) => response.data),
  changeRole: (sessionId: string, role: ProjectSession['role'], confirmed: boolean) =>
    apiClient.post<ProjectSession>(`/sessions/${sessionId}/role`, {
      role,
      actor: 'user',
      confirmed,
    }).then((response) => response.data),
  messages: (sessionId: string) =>
    apiClient.get<ChatMessage[]>(`/sessions/${sessionId}/messages`).then((response) => response.data),
  sendMessage: (sessionId: string, content: string, nodeId?: string) =>
    apiClient.post<ChatMessage>(`/sessions/${sessionId}/converse`, { content, node_id: nodeId })
      .then((response) => response.data),
};

/** Plan-map endpoints; project/node inputs exit as progressive map slices. */
export const projectMapApi = {
  overview: (projectId: string) =>
    apiClient.get<PlanMapOverview>(`/projects/${projectId}/map/overview`)
      .then((response) => response.data),
  children: (projectId: string, parentId: string | null, cursor?: string, limit = 100) =>
    apiClient.get<PlanMapPage>(`/projects/${projectId}/map/children`, {
      params: { parent_id: parentId, cursor, limit },
    }).then((response) => response.data),
  node: (projectId: string, nodeId: string) =>
    apiClient.get<PlanMapNode>(`/projects/${projectId}/map/nodes/${nodeId}`)
      .then((response) => response.data),
  changes: (projectId: string, afterSeq: number, limit = 100, signal?: AbortSignal) =>
    apiClient.get<PlanMapChanges>(`/projects/${projectId}/map/changes`, {
      params: { after_seq: afterSeq, limit },
      signal,
    }).then((response) => response.data),
  codeEntities: (projectId: string, cursor?: string, limit = 200) =>
    apiClient.get<CodeEntityPage>(`/projects/${projectId}/map/code-entities`, {
      params: { cursor, limit },
    }).then((response) => response.data),
  pathSlice: (projectId: string, path: string, signal?: AbortSignal) =>
    apiClient.get<PathSlice>(`/projects/${projectId}/map/path-slice`, {
      params: { path },
      signal,
    }).then((response) => response.data),
  codeRelations: (projectId: string, cursor?: string, limit = 200) =>
    apiClient.get<CodeRelationPage>(`/projects/${projectId}/map/code-relations`, {
      params: { cursor, limit },
    }).then((response) => response.data),
  semanticAnnotations: (projectId: string, cursor?: string, limit = 200) =>
    apiClient.get<SemanticAnnotationPage>(`/projects/${projectId}/map/semantic-annotations`, {
      params: { cursor, limit },
    }).then((response) => response.data),
  blindSpots: (projectId: string, status = 'open', limit = 100) =>
    apiClient.get<BlindSpotPage>(`/projects/${projectId}/map/blind-spots`, {
      params: { status, limit },
    }).then((response) => response.data),
  boundaries: (projectId: string, limit = 10) =>
    apiClient.get<BoundaryOverview>(`/projects/${projectId}/map/boundaries`, {
      params: { limit },
    }).then((response) => response.data),
  arbitration: (projectId: string) =>
    apiClient.get<MapArbitrationPage>(`/projects/${projectId}/map/arbitration`)
      .then((response) => response.data),
};
