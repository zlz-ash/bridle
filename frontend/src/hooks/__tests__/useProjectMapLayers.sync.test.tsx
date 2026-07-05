import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectMapLayers } from '../useProjectMapLayers';
import { projectMapApi } from '../../api/endpoints';

vi.mock('../../api/endpoints', () => ({
  projectMapApi: {
    overview: vi.fn(),
    changes: vi.fn(),
    pathSlice: vi.fn(),
    codeEntities: vi.fn(),
    codeRelations: vi.fn(),
    semanticAnnotations: vi.fn(),
    blindSpots: vi.fn(),
    boundaries: vi.fn(),
    arbitration: vi.fn(),
  },
}));

function wrapper(client: QueryClient) {
  return function Provider({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

describe('useProjectMapLayers sync recovery', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(projectMapApi.codeEntities).mockResolvedValue({
      items: [{ id: 'e-1', path: 'a.py', kind: 'file', name: 'a.py', parent_id: null, payload: {} }],
      next_cursor: null,
      has_more: false,
    });
    vi.mocked(projectMapApi.codeRelations).mockResolvedValue({ items: [], next_cursor: null, has_more: false });
    vi.mocked(projectMapApi.semanticAnnotations).mockResolvedValue({ items: [], next_cursor: null, has_more: false });
    vi.mocked(projectMapApi.blindSpots).mockResolvedValue({ items: [], truncated: false });
    vi.mocked(projectMapApi.boundaries).mockResolvedValue({ items: [], debt_nodes: [] });
    vi.mocked(projectMapApi.arbitration).mockResolvedValue({ items: [] });
  });

  it('does not apply fallback invalidations after project switch', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    vi.mocked(projectMapApi.overview).mockResolvedValue({
      project_id: 'project-1',
      change_seq: 20,
      scan_status: 'ready',
      can_chat: true,
      can_edit_plan: true,
      readiness_reason: null,
      plan_node_count: 0,
      code_entity_count: 1,
      roots: [],
    });
    vi.mocked(projectMapApi.changes).mockImplementation(
      () => new Promise((_resolve, reject) => {
        setTimeout(() => reject(new Error('slow_failure')), 50);
      }),
    );

    const invalidateSpy = vi.spyOn(client, 'invalidateQueries');
    const { rerender } = renderHook(
      ({ projectId }) => useProjectMapLayers(projectId),
      { wrapper: wrapper(client), initialProps: { projectId: 'project-1' as string | null } },
    );

    await waitFor(() => expect(vi.mocked(projectMapApi.overview)).toHaveBeenCalled());
    client.setQueryData(['project-map-code-entities', 'project-1'], {
      items: [{ id: 'e-1', path: 'a.py', kind: 'file', name: 'a.py', parent_id: null, payload: {} }],
      truncated: false,
    });
    client.setQueryData(['project-map-overview', 'project-1'], {
      project_id: 'project-1',
      change_seq: 25,
      scan_status: 'ready',
      can_chat: true,
      can_edit_plan: true,
      readiness_reason: null,
      plan_node_count: 0,
      code_entity_count: 1,
      roots: [],
    });
    rerender({ projectId: 'project-2' });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 120));
    });

    const projectTwoInvalidations = invalidateSpy.mock.calls.filter(([args]) =>
      (args as { queryKey?: unknown[] }).queryKey?.[1] === 'project-2',
    );
    expect(projectTwoInvalidations).toHaveLength(0);
  });
});
