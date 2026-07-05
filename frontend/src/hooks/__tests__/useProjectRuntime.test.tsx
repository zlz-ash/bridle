import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useProgressivePlanMap, useProjectRuntime } from '../useProjectRuntime';
import { projectMapApi, projectSessionsApi, projectsApi } from '../../api/endpoints';

vi.mock('../../api/endpoints', () => ({
  projectsApi: {
    list: vi.fn(),
    open: vi.fn(),
  },
  projectSessionsApi: {
    list: vi.fn(),
    create: vi.fn(),
    messages: vi.fn(),
    sendMessage: vi.fn(),
    changeRole: vi.fn(),
  },
  projectMapApi: {
    overview: vi.fn(),
    children: vi.fn(),
    changes: vi.fn(),
  },
}));

function wrapper(client: QueryClient) {
  /** Provide isolated server state; client input exits as a hook test wrapper. */
  return function Provider({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe('useProjectRuntime', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(projectsApi.list).mockResolvedValue({ projects: [] });
    vi.mocked(projectSessionsApi.list).mockResolvedValue([]);
    vi.mocked(projectSessionsApi.messages).mockResolvedValue([]);
  });

  it('starts without an active project and blocks chat', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useProjectRuntime(), { wrapper: wrapper(client) });

    await waitFor(() => expect(result.current.projectsQuery.isSuccess).toBe(true));
    expect(result.current.activeProject).toBeNull();
    expect(result.current.activeSession).toBeNull();
    expect(result.current.chatDisabled).toBe(true);
    expect(projectSessionsApi.list).not.toHaveBeenCalled();
  });

  it('activates only after explicit open and creates a planning session', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const project = {
      id: 'project-1', path: 'D:\\workspace', name: 'workspace', available: true,
      scan_status: 'ready', can_chat: true, can_edit_plan: true,
      readiness_reason: null,
      last_opened_at: '2026-01-01T00:00:00Z',
    };
    const session = {
      id: 'session-1', project_id: project.id, project_path: project.path,
      title: 'New conversation', role: 'planning' as const, status: 'active',
      available: true, readonly_reason: null, created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
    vi.mocked(projectsApi.open).mockResolvedValue(project);
    vi.mocked(projectSessionsApi.create).mockResolvedValue(session);
    const { result } = renderHook(() => useProjectRuntime(), { wrapper: wrapper(client) });

    await act(async () => {
      await result.current.openProject('D:\\workspace');
    });
    await act(async () => {
      await result.current.createSession();
    });

    expect(result.current.activeProject?.id).toBe('project-1');
    expect(result.current.activeSession?.role).toBe('planning');
    expect(result.current.chatDisabled).toBe(false);
  });

  it('keeps chat blocked when the backend map gate is not ready', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const project = {
      id: 'project-1', path: 'D:\\workspace', name: 'workspace', available: true,
      scan_status: 'needs_arbitration', can_chat: false, can_edit_plan: false,
      readiness_reason: 'pending_user_decision',
      last_opened_at: '2026-01-01T00:00:00Z',
    };
    const session = {
      id: 'session-1', project_id: project.id, project_path: project.path,
      title: 'New conversation', role: 'planning' as const, status: 'active',
      available: true, readonly_reason: null, created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
    vi.mocked(projectsApi.open).mockResolvedValue(project);
    vi.mocked(projectSessionsApi.create).mockResolvedValue(session);
    const { result } = renderHook(() => useProjectRuntime(), { wrapper: wrapper(client) });

    await act(async () => { await result.current.openProject(project.path); });
    await act(async () => { await result.current.createSession(); });

    expect(result.current.chatDisabled).toBe(true);
    expect(result.current.chatDisabledReason).toBe('pending_user_decision');
  });

  it('sends the explicitly selected execution node with the message', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const project = {
      id: 'project-1', path: 'D:\\workspace', name: 'workspace', available: true,
      scan_status: 'ready', can_chat: true, can_edit_plan: true,
      readiness_reason: null,
      last_opened_at: '2026-01-01T00:00:00Z',
    };
    const session = {
      id: 'session-1', project_id: project.id, project_path: project.path,
      title: 'Execution', role: 'executing' as const, status: 'active',
      available: true, readonly_reason: null, created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z',
    };
    vi.mocked(projectsApi.open).mockResolvedValue(project);
    vi.mocked(projectSessionsApi.create).mockResolvedValue(session);
    vi.mocked(projectSessionsApi.sendMessage).mockResolvedValue({
      id: 'message-1', session_id: session.id, role: 'assistant', content: 'done',
      tool_calls: null, tool_result: null, created_at: '2026-01-01T00:00:00Z',
    });
    const { result } = renderHook(() => useProjectRuntime(), { wrapper: wrapper(client) });
    await act(async () => { await result.current.openProject(project.path); });
    await act(async () => { await result.current.createSession(); });

    await act(async () => { await result.current.sendMessage('execute', 'node-1'); });

    expect(projectSessionsApi.sendMessage).toHaveBeenCalledWith('session-1', 'execute', 'node-1');
  });
});

describe('useProgressivePlanMap', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(projectMapApi.overview).mockResolvedValue({
      project_id: 'project-1', scan_status: 'ready', can_chat: true, can_edit_plan: true,
      readiness_reason: null, plan_node_count: 2,
      code_entity_count: 0, change_seq: 1,
      roots: [{ id: 'root', parent_id: null, order: 0, title: 'Root', goal: 'Root', node_type: 'code_change', status: 'pending', depends_on: [] }],
    });
    vi.mocked(projectMapApi.children).mockResolvedValue({
      items: [{ id: 'child', parent_id: 'root', order: 0, title: 'Child', goal: 'Child', node_type: 'code_change', status: 'pending', depends_on: [] }],
      next_cursor: null,
    });
    vi.mocked(projectMapApi.changes).mockResolvedValue({ items: [], last_seq: 1 });
  });

  it('loads roots then merges only requested children', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useProgressivePlanMap('project-1'), { wrapper: wrapper(client) });

    await waitFor(() => expect(result.current.nodes.map((node) => node.id)).toEqual(['root']));
    await act(async () => {
      await result.current.expand('root');
    });

    expect(result.current.nodes.map((node) => node.id)).toEqual(['root', 'child']);
    expect(projectMapApi.children).toHaveBeenCalledWith('project-1', 'root', undefined, 100);
  });
});
