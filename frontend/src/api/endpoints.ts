import { apiClient } from './client';
import type {
  Task,
  PlanCurrent,
  PlanSummary,
  NodeRead,
  CodingSessionRead,
  ChatMessage,
  NodeAgentRun,
  EligibleNodesResponse,
} from './types';

export const tasksApi = {
  list: () => apiClient.get<Task[]>('/tasks').then((r) => r.data),
  get: (id: string) => apiClient.get<Task>(`/tasks/${id}`).then((r) => r.data),
  create: (body: { title: string; goal?: string }) =>
    apiClient.post<Task>('/tasks', body).then((r) => r.data),
  importPlan: (taskId: string, body: import('./types').PlanImportPayload) =>
    apiClient.post<{ plan_id: string }>(`/tasks/${taskId}/plan/import`, body).then((r) => r.data),
  graph: (id: string) =>
    apiClient.get<{ nodes: NodeRead[]; edges: { from: string; to: string }[] }>(
      `/tasks/${id}/graph`,
    ).then((r) => r.data),
};

export const planApi = {
  current: () => apiClient.get<PlanCurrent>('/plan/current').then((r) => r.data),
  summary: () => apiClient.get<PlanSummary>('/plan/current/summary').then((r) => r.data),
};

export const nodesApi = {
  get: (id: string) => apiClient.get<NodeRead>(`/nodes/${id}`).then((r) => r.data),
  runs: (id: string) =>
    apiClient.get<NodeAgentRun[]>(`/nodes/${id}/runs`).then((r) => r.data),
  rerun: (id: string) =>
    apiClient.post<NodeAgentRun>(`/nodes/${id}/run`).then((r) => r.data),
};

export const codingSessionsApi = {
  create: (body: { plan_id: string; auto_continue_budget?: number }) =>
    apiClient.post<CodingSessionRead>('/agent/coding-sessions', body).then((r) => r.data),
  get: (id: string) =>
    apiClient.get<CodingSessionRead>(`/agent/coding-sessions/${id}`).then((r) => r.data),
  cancel: (id: string) =>
    apiClient.post<CodingSessionRead>(`/agent/coding-sessions/${id}/cancel`).then((r) => r.data),
  messages: (id: string) =>
    apiClient
      .get<ChatMessage[]>(`/agent/coding-sessions/${id}/messages`)
      .then((r) => r.data),
  sendMessage: (id: string, content: string) =>
    apiClient
      .post<ChatMessage>(`/agent/coding-sessions/${id}/messages`, {
        role: 'user',
        content,
        tool_calls: null,
        tool_result: null,
      })
      .then((r) => r.data),
  eligibleNodes: (id: string) =>
    apiClient
      .get<EligibleNodesResponse>(`/agent/coding-sessions/${id}/eligible-nodes`)
      .then((r) => r.data),
};

export const nodeAgentRunsApi = {
  get: (runId: string) =>
    apiClient.get<NodeAgentRun>(`/node-agent-runs/${runId}`).then((r) => r.data),
  cancel: (runId: string) =>
    apiClient.post<NodeAgentRun>(`/node-agent-runs/${runId}/cancel`).then((r) => r.data),
};
