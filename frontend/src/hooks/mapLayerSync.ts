import type { QueryClient } from '@tanstack/react-query';
import type { CodeEntity, SemanticAnnotation } from '../api/types';
import type { PagedResult } from './projectMapPaging';
import { isSyncAbortError, throwIfAborted } from './mapSyncAbort';
import { syncMapWatermark, type SyncMapWatermarkDeps } from './syncMapWatermark';

export const MAP_LAYER_QUERY_KEYS = [
  'project-map-code-entities',
  'project-map-code-relations',
  'project-map-semantic-annotations',
  'project-map-blind-spots',
  'project-map-boundaries',
  'project-map-arbitration',
] as const;

export type MapSyncSuccessResult = {
  status: 'success';
  watermark: number;
  entities: CodeEntity[];
  truncated: boolean;
  invalidateAnnotations: boolean;
  invalidateRelations: boolean;
  invalidateBlindSpots: boolean;
  invalidateBoundaries: boolean;
};

export type MapSyncFallbackResult = {
  status: 'fallback';
  reason: string;
  watermark: number;
  needsRetry: boolean;
};

export type MapSyncAttemptResult = MapSyncSuccessResult | MapSyncFallbackResult;

export type MapSyncAttemptInput = {
  projectId: string;
  fromSeq: number;
  targetSeq: number;
  queryClient: QueryClient;
  deps: SyncMapWatermarkDeps;
  signal?: AbortSignal;
};

async function invalidateLayer(
  queryClient: QueryClient,
  key: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<void> {
  throwIfAborted(signal);
  await queryClient.invalidateQueries({ queryKey: [key, projectId] });
  throwIfAborted(signal);
}

/** Invalidate every derived map layer; never throws except on abort. */
export async function safeInvalidateAllMapLayers(
  queryClient: QueryClient,
  projectId: string,
  signal?: AbortSignal,
): Promise<{ ok: true } | { ok: false; reason: 'layer_invalidate_failed' | 'sync_aborted' }> {
  try {
    throwIfAborted(signal);
    await Promise.all(
      MAP_LAYER_QUERY_KEYS.map((key) => invalidateLayer(queryClient, key, projectId, signal)),
    );
    return { ok: true };
  } catch (error) {
    if (isSyncAbortError(error)) {
      return { ok: false, reason: 'sync_aborted' };
    }
    return { ok: false, reason: 'layer_invalidate_failed' };
  }
}

/** Run one incremental sync attempt; all failures return structured fallback results. */
export async function attemptMapLayerSync(
  input: MapSyncAttemptInput,
): Promise<MapSyncAttemptResult> {
  const { projectId, fromSeq, targetSeq, queryClient, deps, signal } = input;
  throwIfAborted(signal);
  const entityCache = queryClient.getQueryData<PagedResult<CodeEntity>>([
    'project-map-code-entities',
    projectId,
  ]);
  const annotationCache = queryClient.getQueryData<PagedResult<SemanticAnnotation>>([
    'project-map-semantic-annotations',
    projectId,
  ]);

  if (!entityCache) {
    const invalidated = await safeInvalidateAllMapLayers(queryClient, projectId, signal);
    if (!invalidated.ok && invalidated.reason === 'sync_aborted') {
      return {
        status: 'fallback',
        reason: 'sync_aborted',
        watermark: fromSeq,
        needsRetry: false,
      };
    }
    return {
      status: 'fallback',
      reason: invalidated.ok ? 'entity_cache_missing' : invalidated.reason,
      watermark: fromSeq,
      needsRetry: true,
    };
  }

  try {
    const result = await syncMapWatermark(
      fromSeq,
      targetSeq,
      {
        entities: entityCache.items,
        annotations: annotationCache?.items ?? [],
      },
      { ...deps, signal },
    );
    throwIfAborted(signal);

    if (!result.incremental) {
      const invalidated = await safeInvalidateAllMapLayers(queryClient, projectId, signal);
      if (!invalidated.ok && invalidated.reason === 'sync_aborted') {
        return {
          status: 'fallback',
          reason: 'sync_aborted',
          watermark: fromSeq,
          needsRetry: false,
        };
      }
      return {
        status: 'fallback',
        reason: invalidated.ok
          ? (result.fallbackReason ?? 'unsupported_change_event')
          : invalidated.reason,
        watermark: result.watermark,
        needsRetry: true,
      };
    }

    return {
      status: 'success',
      watermark: result.watermark,
      entities: result.cache.entities,
      truncated: entityCache.truncated ?? false,
      invalidateAnnotations: result.invalidateAnnotations,
      invalidateRelations: result.invalidateRelations,
      invalidateBlindSpots: result.invalidateBlindSpots,
      invalidateBoundaries: result.invalidateBoundaries,
    };
  } catch (error) {
    if (isSyncAbortError(error)) {
      return {
        status: 'fallback',
        reason: 'sync_aborted',
        watermark: fromSeq,
        needsRetry: false,
      };
    }
    const invalidated = await safeInvalidateAllMapLayers(queryClient, projectId, signal);
    if (!invalidated.ok && invalidated.reason === 'sync_aborted') {
      return {
        status: 'fallback',
        reason: 'sync_aborted',
        watermark: fromSeq,
        needsRetry: false,
      };
    }
    return {
      status: 'fallback',
      reason: invalidated.ok ? 'change_apply_failed' : invalidated.reason,
      watermark: fromSeq,
      needsRetry: true,
    };
  }
}

export type ApplyPostSyncInput = {
  projectId: string;
  fromSeq: number;
  queryClient: QueryClient;
  result: MapSyncSuccessResult;
  signal?: AbortSignal;
};

/** Apply entity cache updates and derived-layer invalidations after a successful sync. */
export async function applyPostSyncUpdates(
  input: ApplyPostSyncInput,
): Promise<MapSyncAttemptResult> {
  const { projectId, fromSeq, queryClient, result, signal } = input;
  try {
    throwIfAborted(signal);
    queryClient.setQueryData(['project-map-code-entities', projectId], {
      items: result.entities,
      truncated: result.truncated,
    });
    const invalidations: Promise<unknown>[] = [];
    if (result.invalidateAnnotations) {
      invalidations.push(
        invalidateLayer(queryClient, 'project-map-semantic-annotations', projectId, signal),
      );
    }
    if (result.invalidateRelations) {
      invalidations.push(invalidateLayer(queryClient, 'project-map-code-relations', projectId, signal));
    }
    if (result.invalidateBlindSpots) {
      invalidations.push(invalidateLayer(queryClient, 'project-map-blind-spots', projectId, signal));
    }
    if (result.invalidateBoundaries) {
      invalidations.push(invalidateLayer(queryClient, 'project-map-boundaries', projectId, signal));
    }
    await Promise.all(invalidations);
    throwIfAborted(signal);
    return result;
  } catch (error) {
    if (isSyncAbortError(error)) {
      return {
        status: 'fallback',
        reason: 'sync_aborted',
        watermark: fromSeq,
        needsRetry: false,
      };
    }
    const invalidated = await safeInvalidateAllMapLayers(queryClient, projectId, signal);
    if (!invalidated.ok && invalidated.reason === 'sync_aborted') {
      return {
        status: 'fallback',
        reason: 'sync_aborted',
        watermark: fromSeq,
        needsRetry: false,
      };
    }
    return {
      status: 'fallback',
      reason: invalidated.ok ? 'derived_layer_invalidate_failed' : invalidated.reason,
      watermark: fromSeq,
      needsRetry: true,
    };
  }
}

/** @deprecated Use safeInvalidateAllMapLayers */
export async function invalidateAllMapLayers(
  queryClient: QueryClient,
  projectId: string,
): Promise<void> {
  const result = await safeInvalidateAllMapLayers(queryClient, projectId);
  if (!result.ok) {
    throw new Error(result.reason);
  }
}
