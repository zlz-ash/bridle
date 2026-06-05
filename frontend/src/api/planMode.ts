import { apiClient } from './client';
import type { PlanImportPayload, PlanModeConverseResponse } from './types';

export type ChatTurn = { role: 'user' | 'assistant'; content: string };

export const planModeApi = {
  converse: (history: ChatTurn[], workspaceOverview: Record<string, unknown>) =>
    apiClient
      .post<PlanModeConverseResponse>(
        '/plan-mode/converse',
        { history, workspace_overview: workspaceOverview },
        { timeout: 60_000 },
      )
      .then((r) => r.data),
};

export const workspaceApi = {
  overview: () =>
    apiClient
      .get<Record<string, unknown>>('/workspace/overview')
      .then((r) => r.data),
};
