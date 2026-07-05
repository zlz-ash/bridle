import { QueryClient } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import {
  applyPostSyncUpdates,
  attemptMapLayerSync,
  safeInvalidateAllMapLayers,
  MAP_LAYER_QUERY_KEYS,
  type MapSyncSuccessResult,
} from '../mapLayerSync';

describe('mapLayerSync', () => {
  it('invalidates every derived map layer for one project', async () => {
    const queryClient = new QueryClient();
    const invalidateSpy = vi.spyOn(queryClient, 'invalidateQueries');

    await safeInvalidateAllMapLayers(queryClient, 'project-1').then((result) => {
      expect(result.ok).toBe(true);
    });

    expect(invalidateSpy).toHaveBeenCalledTimes(MAP_LAYER_QUERY_KEYS.length);
  });

  it('returns fallback when changes fail without throwing', async () => {
    const queryClient = new QueryClient();
    queryClient.setQueryData(['project-map-code-entities', 'project-1'], {
      items: [{ id: 'e-1', path: 'a.py', kind: 'file', name: 'a.py', parent_id: null, payload: {} }],
      truncated: false,
    });

    const result = await attemptMapLayerSync({
      projectId: 'project-1',
      fromSeq: 10,
      targetSeq: 15,
      queryClient,
      deps: {
        fetchChanges: async () => {
          throw new Error('network_failed');
        },
        fetchPathSlice: async () => ({
          path: 'a.py',
          entities: [],
          relations: [],
          blind_spots: [],
        }),
      },
    });

    expect(result.status).toBe('fallback');
    if (result.status === 'fallback') {
      expect(result.reason).toBe('change_apply_failed');
      expect(result.needsRetry).toBe(true);
      expect(result.watermark).toBe(10);
    }
  });

  it('returns fallback when derived layer invalidation fails after success', async () => {
    const queryClient = new QueryClient();
    vi.spyOn(queryClient, 'invalidateQueries').mockRejectedValueOnce(new Error('refetch_failed'));

    const syncResult: MapSyncSuccessResult = {
      status: 'success',
      watermark: 6,
      entities: [{ id: 'e-1', path: 'a.py', kind: 'file', name: 'a.py', parent_id: null, payload: {} }],
      truncated: false,
      invalidateAnnotations: true,
      invalidateRelations: false,
      invalidateBlindSpots: false,
      invalidateBoundaries: false,
    };

    const applied = await applyPostSyncUpdates({
      projectId: 'project-1',
      fromSeq: 4,
      queryClient,
      result: syncResult,
    });
    expect(applied.status).toBe('fallback');
    if (applied.status === 'fallback') {
      expect(applied.reason).toBe('derived_layer_invalidate_failed');
      expect(applied.watermark).toBe(4);
    }
  });
});
