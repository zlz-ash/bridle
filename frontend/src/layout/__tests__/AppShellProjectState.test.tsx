import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from '../AppShell';
import { useProgressivePlanMap, useProjectRuntime } from '../../hooks/useProjectRuntime';

vi.mock('../../hooks/useProjectRuntime', () => ({
  useProjectRuntime: vi.fn(),
  useProgressivePlanMap: vi.fn(),
}));

function wrapper({ children }: { children: ReactNode }) {
  /** Provide isolated query state; children input exits inside the application provider. */
  return <QueryClientProvider client={new QueryClient()}>{children}</QueryClientProvider>;
}

function runtime(overrides: Record<string, unknown> = {}) {
  /** Build shell runtime state; override input exits as a complete hook-shaped fixture. */
  return {
    projectsQuery: { isLoading: false, isError: false },
    sessionsQuery: { isLoading: false, isError: false },
    messagesQuery: { isLoading: false, isError: false },
    activeProject: null,
    activeSession: null,
    projects: [],
    sessions: [],
    messages: [],
    chatDisabled: true,
    selectProject: vi.fn(),
    selectSession: vi.fn(),
    openProject: vi.fn(),
    createSession: vi.fn(),
    changeRole: vi.fn(),
    sendMessage: vi.fn(),
    openMutation: { isPending: false, isError: false },
    createSessionMutation: { isPending: false, isError: false },
    roleMutation: { isPending: false, isError: false },
    sendMutation: { isPending: false, isError: false },
    ...overrides,
  };
}

describe('AppShell project state', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useProgressivePlanMap).mockReturnValue({
      nodes: [],
      expand: vi.fn(),
      overviewQuery: { isLoading: false, isError: false },
      changesQuery: { isLoading: false, isError: false },
      changeSeq: 0,
    } as never);
  });

  it('starts project-less and blocks conversation input', () => {
    vi.mocked(useProjectRuntime).mockReturnValue(runtime() as never);

    render(<AppShell />, { wrapper });

    expect(screen.getByText('Open a project to begin')).not.toBeNull();
    expect(screen.getByRole('textbox')).toHaveProperty('disabled', true);
  });

  it('offers a new conversation after a project is selected', () => {
    const createSession = vi.fn();
    vi.mocked(useProjectRuntime).mockReturnValue(runtime({
      activeProject: {
        id: 'project-1', name: 'workspace', path: 'D:\\workspace', available: true,
        scan_status: 'completed', last_opened_at: '2026-01-01T00:00:00Z',
      },
      createSession,
    }) as never);

    render(<AppShell />, { wrapper });
    fireEvent.click(screen.getByRole('button', { name: 'New conversation' }));

    expect(createSession).toHaveBeenCalledOnce();
    expect(screen.getByRole('textbox')).toHaveProperty('disabled', true);
  });

  it('asks for confirmation before entering execution', () => {
    const changeRole = vi.fn();
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(useProjectRuntime).mockReturnValue(runtime({
      activeProject: {
        id: 'project-1', name: 'workspace', path: 'D:\\workspace', available: true,
        scan_status: 'completed', last_opened_at: '2026-01-01T00:00:00Z',
      },
      activeSession: {
        id: 'session-1', project_id: 'project-1', project_path: 'D:\\workspace',
        title: 'New conversation', role: 'planning', status: 'active', available: true,
        readonly_reason: null, created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z',
      },
      chatDisabled: false,
      changeRole,
    }) as never);

    render(<AppShell />, { wrapper });
    fireEvent.click(screen.getByRole('button', { name: 'Enter execution' }));

    expect(window.confirm).toHaveBeenCalledOnce();
    expect(changeRole).toHaveBeenCalledWith('executing', true);
  });
});
