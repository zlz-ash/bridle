import type { CodeEntity, PlanMapChanges, PathSlice } from '../api/types';
import {
  applyChangeEvents,
  mergeEntitiesById,
  type ApplyChangesResult,
  type MapLayerCache,
} from './applyMapChanges';
import { isSyncAbortError, throwIfAborted } from './mapSyncAbort';

export type SyncMapWatermarkDeps = {
  fetchChanges: (afterSeq: number, limit: number, signal?: AbortSignal) => Promise<PlanMapChanges>;
  fetchPathSlice: (path: string, signal?: AbortSignal) => Promise<PathSlice>;
  pageSize?: number;
  signal?: AbortSignal;
};

export type SyncMapWatermarkResult = {
  cache: MapLayerCache;
  watermark: number;
  incremental: boolean;
  invalidateAnnotations: boolean;
  invalidateRelations: boolean;
  invalidateBlindSpots: boolean;
  invalidateBoundaries: boolean;
  fallbackReason: string | null;
};

function mergeApplyFlags(left: ApplyChangesResult, right: ApplyChangesResult): ApplyChangesResult {
  return {
    cache: right.cache,
    incremental: left.incremental && right.incremental,
    needsEntityPage: left.needsEntityPage || right.needsEntityPage,
    invalidateAnnotations: left.invalidateAnnotations || right.invalidateAnnotations,
    invalidateRelations: left.invalidateRelations || right.invalidateRelations,
    invalidateBlindSpots: left.invalidateBlindSpots || right.invalidateBlindSpots,
    invalidateBoundaries: left.invalidateBoundaries || right.invalidateBoundaries,
    refreshPaths: [...new Set([...left.refreshPaths, ...right.refreshPaths])],
  };
}

/** Paginate change events until target seq; advance watermark only after each page applies. */
export async function syncMapWatermark(
  fromSeq: number,
  targetSeq: number,
  cache: MapLayerCache,
  deps: SyncMapWatermarkDeps,
): Promise<SyncMapWatermarkResult> {
  const pageSize = deps.pageSize ?? 100;
  const signal = deps.signal;
  let watermark = fromSeq;
  let workingCache = cache;
  let aggregate: ApplyChangesResult = {
    cache,
    incremental: true,
    needsEntityPage: false,
    invalidateAnnotations: false,
    invalidateRelations: false,
    invalidateBlindSpots: false,
    invalidateBoundaries: false,
    refreshPaths: [],
  };

  while (watermark < targetSeq) {
    throwIfAborted(signal);
    const changes = await deps.fetchChanges(watermark, pageSize, signal);
    throwIfAborted(signal);
    if (changes.items.length === 0) {
      watermark = targetSeq;
      break;
    }
    const applied = applyChangeEvents(workingCache, changes.items);
    aggregate = mergeApplyFlags(aggregate, applied);
    workingCache = applied.cache;
    if (!applied.incremental) {
      return {
        cache: workingCache,
        watermark: changes.last_seq,
        incremental: false,
        invalidateAnnotations: aggregate.invalidateAnnotations,
        invalidateRelations: aggregate.invalidateRelations,
        invalidateBlindSpots: aggregate.invalidateBlindSpots,
        invalidateBoundaries: aggregate.invalidateBoundaries,
        fallbackReason: 'unsupported_change_event',
      };
    }
    watermark = changes.last_seq;
    if (changes.items.length < pageSize && watermark >= targetSeq) {
      break;
    }
    if (changes.items.length < pageSize && watermark < targetSeq) {
      return {
        cache: workingCache,
        watermark,
        incremental: false,
        invalidateAnnotations: aggregate.invalidateAnnotations,
        invalidateRelations: aggregate.invalidateRelations,
        invalidateBlindSpots: aggregate.invalidateBlindSpots,
        invalidateBoundaries: aggregate.invalidateBoundaries,
        fallbackReason: 'change_seq_gap',
      };
    }
  }

  let entities = workingCache.entities;
  if (aggregate.needsEntityPage) {
    for (const path of aggregate.refreshPaths) {
      throwIfAborted(signal);
      const slice = await deps.fetchPathSlice(path, signal);
      throwIfAborted(signal);
      entities = mergeEntitiesById(entities, slice.entities);
    }
  }

  return {
    cache: { ...workingCache, entities },
    watermark: Math.min(watermark, targetSeq),
    incremental: true,
    invalidateAnnotations: aggregate.invalidateAnnotations,
    invalidateRelations: aggregate.invalidateRelations,
    invalidateBlindSpots: aggregate.invalidateBlindSpots,
    invalidateBoundaries: aggregate.invalidateBoundaries,
    fallbackReason: null,
  };
}
