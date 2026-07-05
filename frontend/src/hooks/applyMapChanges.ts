import type { CodeEntity, PlanMapChanges, SemanticAnnotation } from '../api/types';

export type MapLayerCache = {
  entities: CodeEntity[];
  annotations: SemanticAnnotation[];
};

export type ApplyChangesResult = {
  cache: MapLayerCache;
  incremental: boolean;
  needsEntityPage: boolean;
  invalidateAnnotations: boolean;
  invalidateRelations: boolean;
  invalidateBlindSpots: boolean;
  invalidateBoundaries: boolean;
  refreshPaths: string[];
};

export function removeEntitiesForPath(entities: CodeEntity[], path: string): CodeEntity[] {
  return entities.filter(
    (entity) => entity.path !== path && !entity.path.startsWith(`${path}::`),
  );
}

export function mergeEntitiesById(existing: CodeEntity[], incoming: CodeEntity[]): CodeEntity[] {
  const merged = new Map(existing.map((entity) => [entity.id, entity]));
  for (const entity of incoming) {
    merged.set(entity.id, entity);
  }
  return [...merged.values()].sort((left, right) => left.path.localeCompare(right.path));
}

/** Apply map change events to an in-memory cache; returns whether a full refetch is required. */
export function applyChangeEvents(
  cache: MapLayerCache,
  events: PlanMapChanges['items'],
): ApplyChangesResult {
  if (events.length === 0) {
    return {
      cache,
      incremental: true,
      needsEntityPage: false,
      invalidateAnnotations: false,
      invalidateRelations: false,
      invalidateBlindSpots: false,
      invalidateBoundaries: false,
      refreshPaths: [],
    };
  }

  let entities = cache.entities;
  let incremental = true;
  let needsEntityPage = false;
  let invalidateAnnotations = false;
  let invalidateRelations = false;
  let invalidateBlindSpots = false;
  let invalidateBoundaries = false;
  const refreshPaths: string[] = [];

  for (const event of events) {
    if (event.entity_type === 'code_entity' && event.operation === 'refresh') {
      const path = String(event.payload.path ?? '');
      if (!path) {
        incremental = false;
        continue;
      }
      entities = removeEntitiesForPath(entities, path);
      needsEntityPage = true;
      invalidateRelations = true;
      invalidateBlindSpots = true;
      invalidateBoundaries = true;
      invalidateAnnotations = true;
      refreshPaths.push(path);
      continue;
    }
    if (event.entity_type === 'semantic_annotation') {
      invalidateAnnotations = true;
      continue;
    }
    if (event.entity_type === 'map_objection' || event.entity_type === 'project_map') {
      incremental = false;
    }
  }

  return {
    cache: { ...cache, entities },
    incremental,
    needsEntityPage,
    invalidateAnnotations,
    invalidateRelations,
    invalidateBlindSpots,
    invalidateBoundaries,
    refreshPaths,
  };
}
