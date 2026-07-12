import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectMapLayers } from '../useProjectMapLayers';
import { projectMapApi } from '../../api/endpoints';
import {
  clearMapSyncLogEvents,
  configureMapSyncLogSink,
  getMapSyncLogEvents,
} from '../mapSyncLogger';
import { MAP_LAYER_QUERY_KEYS } from '../mapLayerSync';
import { MAP_SYNC_RETRY_BASE_MS } from '../mapSyncRetry';

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

const overviewPayload = (changeSeq: number, projectId = 'project-1') => ({
  project_id: projectId,
  change_seq: changeSeq,
  scan_status: 'ready',
  can_chat: true,
  can_edit_plan: true,
  readiness_reason: null,
  plan_node_count: 0,
  code_entity_count: 1,
  roots: [],
});

function wrapper(client: QueryClient) {
  return function Provider({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

function seedEntityCache(client: QueryClient, projectId: string) {
  client.setQueryData(['project-map-code-entities', projectId], {
    items: [{ id: 'e-1', path: 'a.py', kind: 'file', name: 'a.py', parent_id: null, payload: {} }],
    truncated: false,
  });
}

async function bumpOverviewSeq(client: QueryClient, projectId: string, nextSeq: number) {
  await act(async () => {
    client.setQueryData(['project-map-overview', projectId], overviewPayload(nextSeq));
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

async function advanceRetryTimers(steps = 1) {
  await act(async () => {
    for (let index = 0; index < steps; index += 1) {
      vi.advanceTimersByTime(MAP_SYNC_RETRY_BASE_MS * 2 ** index);
      await Promise.resolve();
    }
  });
}

describe('useProjectMapLayers retry scheduling', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.clearAllMocks();
    configureMapSyncLogSink({ enabled: true, maxEvents: 64 });
    clearMapSyncLogEvents();
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

  afterEach(() => {
    configureMapSyncLogSink(null);
    vi.useRealTimers();
  });

  it('recovers after changes failure using fake timer backoff', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    let changesCalls = 0;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockImplementation(async () => {
      changesCalls += 1;
      if (changesCalls === 1) {
        throw new Error('network_failed');
      }
      return { items: [], last_seq: overviewSeq };
    });

    const { result } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);

    await waitFor(() => expect(result.current.fetchFallbackReason).toBe('change_apply_failed'));
    await advanceRetryTimers(1);
    await waitFor(() => expect(changesCalls).toBe(2));
    await waitFor(() => expect(result.current.fetchFallbackReason).toBeNull());
    const events = getMapSyncLogEvents();
    expect(events.some((event) => event.type === 'recovered')).toBe(true);
    expect(events.every((event) => (
      typeof event.stage === 'string'
      && typeof event.status === 'string'
      && typeof event.durationMs === 'number'
      && event.durationMs >= 0
    ))).toBe(true);
  });

  it('recovers after path-slice failure', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    let pathSliceCalls = 0;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockResolvedValue({
      items: [
        {
          change_seq: 11,
          entity_type: 'code_entity',
          entity_id: 'e-1',
          operation: 'refresh',
          payload: { path: 'a.py' },
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      last_seq: 15,
    });
    vi.mocked(projectMapApi.pathSlice).mockImplementation(async () => {
      pathSliceCalls += 1;
      if (pathSliceCalls === 1) {
        throw new Error('path_slice_failed');
      }
      return { path: 'a.py', entities: [], relations: [], blind_spots: [] };
    });

    const { result } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);

    await waitFor(() => expect(result.current.fetchFallbackReason).toBe('change_apply_failed'));
    await advanceRetryTimers(1);
    await waitFor(() => expect(pathSliceCalls).toBe(2));
    await waitFor(() => expect(result.current.fetchFallbackReason).toBeNull());
  });

  it('recovers after fallback layer invalidation failure when entity cache is missing', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    let invalidateCalls = 0;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.codeEntities).mockImplementation(() => new Promise(() => {}));
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries').mockImplementation(async () => {
      invalidateCalls += 1;
      if (invalidateCalls === 1) {
        throw new Error('refetch_failed');
      }
    });

    const { result } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);

    await waitFor(() => expect(result.current.fetchFallbackReason).toBe('layer_invalidate_failed'));
    await advanceRetryTimers(1);
    await waitFor(() => expect(invalidateCalls).toBeGreaterThan(1));
    invalidateSpy.mockRestore();
  });

  it('recovers after post-sync derived layer invalidation failure', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    let invalidateCalls = 0;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockResolvedValue({
      items: [
        {
          change_seq: 11,
          entity_type: 'semantic_annotation',
          entity_id: 'ann-1',
          operation: 'refresh',
          payload: {},
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      last_seq: 15,
    });
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries').mockImplementation(async (filters) => {
      const key = (filters as { queryKey?: unknown[] }).queryKey?.[0];
      if (key === 'project-map-semantic-annotations') {
        invalidateCalls += 1;
        if (invalidateCalls === 1) {
          throw new Error('annotation_refetch_failed');
        }
      }
    });

    const { result } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);

    await waitFor(() => expect(result.current.fetchFallbackReason).toBe('derived_layer_invalidate_failed'));
    await advanceRetryTimers(1);
    await waitFor(() => expect(result.current.fetchFallbackReason).toBeNull());
    invalidateSpy.mockRestore();
  });

  it('limits retry frequency while failures continue', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 12;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockRejectedValue(new Error('still_failing'));

    const { result } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(12));
    seedEntityCache(client, 'project-1');
    overviewSeq = 20;
    await bumpOverviewSeq(client, 'project-1', 20);

    await waitFor(() => expect(result.current.fetchFallbackReason).toBe('change_apply_failed'));
    expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(1);

    await act(async () => {
      vi.advanceTimersByTime(500);
      await Promise.resolve();
    });
    expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(1);

    await advanceRetryTimers(1);
    await waitFor(() => expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(2));
  });

  it('cancels pending retry timers when project switches', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    vi.mocked(projectMapApi.overview).mockImplementation(async (projectId: string) =>
      overviewPayload(projectId === 'project-2' ? 5 : overviewSeq, projectId),
    );
    vi.mocked(projectMapApi.changes).mockRejectedValue(new Error('slow_failure'));

    const { rerender, result } = renderHook(
      ({ projectId }) => useProjectMapLayers(projectId),
      { wrapper: wrapper(client), initialProps: { projectId: 'project-1' as string | null } },
    );

    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 18;
    await bumpOverviewSeq(client, 'project-1', 18);

    await waitFor(() => expect(getMapSyncLogEvents().some((event) => event.type === 'retry_scheduled')).toBe(true));
    const callsBeforeSwitch = vi.mocked(projectMapApi.changes).mock.calls.length;

    rerender({ projectId: 'project-2' });
    await advanceRetryTimers(3);
    expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(callsBeforeSwitch);
  });

  it('aborts in-flight sync on unmount without writing cache, state, or logs', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    const pending = deferred<{ items: []; last_seq: number }>();
    let capturedSignal: AbortSignal | undefined;
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockImplementation((_projectId, _afterSeq, _limit, signal) => {
      capturedSignal = signal;
      return pending.promise;
    });

    const { result, unmount } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    const beforeEntities = client.getQueryData(['project-map-code-entities', 'project-1']);
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);
    await waitFor(() => expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(1));
    expect(capturedSignal?.aborted).toBe(false);

    const eventsBeforeUnmount = getMapSyncLogEvents().length;
    unmount();
    expect(capturedSignal?.aborted).toBe(true);
    pending.resolve({ items: [], last_seq: 15 });
    await act(async () => {
      await Promise.resolve();
    });

    expect(client.getQueryData(['project-map-code-entities', 'project-1'])).toEqual(beforeEntities);
    expect(getMapSyncLogEvents().length).toBe(eventsBeforeUnmount);
    expect(getMapSyncLogEvents().some((event) => event.type === 'recovered')).toBe(false);
  });

  it('aborts in-flight changes request signal on unmount', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    const changesSignals: AbortSignal[] = [];
    const pendingChanges = deferred<{ items: []; last_seq: number }>();
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockImplementation((_projectId, _afterSeq, _limit, signal) => {
      if (signal) changesSignals.push(signal);
      return pendingChanges.promise;
    });

    const { result, unmount } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);
    await waitFor(() => expect(changesSignals.length).toBeGreaterThan(0));
    expect(changesSignals.some((signal) => signal.aborted)).toBe(false);

    unmount();
    expect(changesSignals.some((signal) => signal.aborted)).toBe(true);
    pendingChanges.resolve({ items: [], last_seq: 15 });
    await act(async () => {
      await Promise.resolve();
    });
  });

  it('aborts in-flight pathSlice request signal on unmount', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    const pathSignals: AbortSignal[] = [];
    const pendingPathSlice = deferred<{ path: string; entities: []; relations: []; blind_spots: [] }>();
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockResolvedValue({
      items: [
        {
          change_seq: 11,
          entity_type: 'code_entity',
          entity_id: 'e-1',
          operation: 'refresh',
          payload: { path: 'a.py' },
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      last_seq: 15,
    });
    vi.mocked(projectMapApi.pathSlice).mockImplementation((_projectId, _path, signal) => {
      if (signal) pathSignals.push(signal);
      return pendingPathSlice.promise;
    });

    const { result, unmount } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    const beforeEntities = client.getQueryData(['project-map-code-entities', 'project-1']);
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);
    await waitFor(() => expect(pathSignals.length).toBeGreaterThan(0));
    expect(pathSignals.some((signal) => signal.aborted)).toBe(false);

    const eventsBeforeUnmount = getMapSyncLogEvents().length;
    unmount();
    expect(pathSignals.some((signal) => signal.aborted)).toBe(true);
    pendingPathSlice.resolve({ path: 'a.py', entities: [], relations: [], blind_spots: [] });
    await act(async () => {
      await Promise.resolve();
    });

    expect(client.getQueryData(['project-map-code-entities', 'project-1'])).toEqual(beforeEntities);
    expect(getMapSyncLogEvents().length).toBe(eventsBeforeUnmount);
    expect(getMapSyncLogEvents().some((event) => event.type === 'recovered')).toBe(false);
  });

  it('cancels in-flight derived layer refetches for all six map query keys on unmount', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    let overviewSeq = 10;
    const pendingAnnotations = deferred<{ items: []; next_cursor: null; has_more: false }>();
    vi.mocked(projectMapApi.overview).mockImplementation(async () => overviewPayload(overviewSeq));
    vi.mocked(projectMapApi.changes).mockResolvedValue({
      items: [
        {
          change_seq: 11,
          entity_type: 'semantic_annotation',
          entity_id: 'ann-1',
          operation: 'refresh',
          payload: {},
          created_at: '2026-01-01T00:00:00Z',
        },
      ],
      last_seq: 15,
    });
    vi.mocked(projectMapApi.semanticAnnotations).mockImplementation(() => pendingAnnotations.promise);
    const cancelSpy = vi.spyOn(client, 'cancelQueries');
    const beforeAnnotations = { items: [{ id: 'ann-old', status: 'active' }], truncated: false };
    client.setQueryData(['project-map-semantic-annotations', 'project-1'], beforeAnnotations);

    const { result, unmount } = renderHook(() => useProjectMapLayers('project-1'), { wrapper: wrapper(client) });
    await waitFor(() => expect(result.current.overviewQuery.data?.change_seq).toBe(10));
    seedEntityCache(client, 'project-1');
    overviewSeq = 15;
    await bumpOverviewSeq(client, 'project-1', 15);
    await waitFor(() => expect(vi.mocked(projectMapApi.changes).mock.calls.length).toBe(1));

    unmount();
    await act(async () => {
      await Promise.resolve();
    });

    for (const key of MAP_LAYER_QUERY_KEYS) {
      expect(cancelSpy).toHaveBeenCalledWith({ queryKey: [key, 'project-1'] });
    }
    pendingAnnotations.resolve({ items: [], next_cursor: null, has_more: false });
    await act(async () => {
      await Promise.resolve();
    });
    expect(client.getQueryData(['project-map-semantic-annotations', 'project-1'])).toEqual(beforeAnnotations);
    cancelSpy.mockRestore();
  });
});
