import { describe, expect, it } from 'vitest';
import { applyChangeEvents, mergeEntitiesById, removeEntitiesForPath } from '../applyMapChanges';
import { syncMapWatermark } from '../syncMapWatermark';
import { ENTITY_CAP, fetchAllPages, PAGE_SIZE } from '../projectMapPaging';
import type { CodeEntity } from '../../api/types';

describe('projectMapPaging', () => {
  it('loads all pages until cursor ends without silent truncation', async () => {
    const pages = Array.from({ length: 12 }, (_, index) =>
      Array.from({ length: PAGE_SIZE }, (_v, offset) => ({ id: `e-${index * PAGE_SIZE + offset}` })),
    );
    const result = await fetchAllPages<{ id: string }>(async (cursor) => {
      const pageIndex = cursor ? Number(cursor) : 0;
      const items = pages[pageIndex] ?? [];
      const nextIndex = pageIndex + 1;
      return {
        items,
        next_cursor: nextIndex < pages.length ? String(nextIndex) : null,
        has_more: nextIndex < pages.length,
      };
    }, ENTITY_CAP);

    expect(result.items).toHaveLength(12 * PAGE_SIZE);
    expect(result.truncated).toBe(false);
  });

  it('marks truncated when cap is exceeded while more pages remain', async () => {
    const result = await fetchAllPages<{ id: string }>(async (cursor) => {
      const page = cursor ? Number(cursor) : 0;
      return {
        items: Array.from({ length: PAGE_SIZE }, (_v, index) => ({ id: `${page}-${index}` })),
        next_cursor: String(page + 1),
        has_more: true,
      };
    }, PAGE_SIZE + 10);

    expect(result.items.length).toBeGreaterThan(PAGE_SIZE);
    expect(result.truncated).toBe(true);
  });
});

describe('applyMapChanges', () => {
  const baseEntity = (id: string, path: string): CodeEntity => ({
    id,
    path,
    kind: 'file',
    name: path,
    parent_id: null,
    payload: {},
  });

  it('removes refreshed path entities locally without full refetch flag', () => {
    const cache = {
      entities: [baseEntity('1', 'a.py'), baseEntity('2', 'a.py::fn')],
      annotations: [],
    };
    const applied = applyChangeEvents(cache, [{
      change_seq: 2,
      entity_type: 'code_entity',
      entity_id: 'x',
      operation: 'refresh',
      payload: { path: 'a.py' },
      created_at: '',
    }]);
    expect(applied.incremental).toBe(true);
    expect(applied.needsEntityPage).toBe(true);
    expect(applied.invalidateRelations).toBe(true);
    expect(applied.invalidateBlindSpots).toBe(true);
    expect(applied.invalidateBoundaries).toBe(true);
    expect(applied.refreshPaths).toEqual(['a.py']);
    expect(applied.cache.entities).toEqual([]);
  });

  it('merges incoming page entities by id', () => {
    const merged = mergeEntitiesById(
      [baseEntity('1', 'a.py')],
      [baseEntity('1', 'a.py'), baseEntity('2', 'b.py')],
    );
    expect(merged.map((entity) => entity.id)).toEqual(['1', '2']);
  });

  it('requires fallback for unsupported project_map events', () => {
    const applied = applyChangeEvents(
      { entities: [baseEntity('1', 'a.py')], annotations: [] },
      [{
        change_seq: 3,
        entity_type: 'project_map',
        entity_id: 'p',
        operation: 'ready',
        payload: {},
        created_at: '',
      }],
    );
    expect(applied.incremental).toBe(false);
  });

  it('filters entities by path prefix', () => {
    const filtered = removeEntitiesForPath(
      [baseEntity('1', 'a.py'), baseEntity('2', 'a.py::fn'), baseEntity('3', 'b.py')],
      'a.py',
    );
    expect(filtered.map((entity) => entity.id)).toEqual(['3']);
  });
});

describe('syncMapWatermark', () => {
  const baseEntity = (id: string, path: string): CodeEntity => ({
    id,
    path,
    kind: 'file',
    name: path,
    parent_id: null,
    payload: {},
  });

  it('paginates more than 100 events without skipping watermark', async () => {
    const events = Array.from({ length: 101 }, (_, index) => ({
      change_seq: index + 1,
      entity_type: 'semantic_annotation',
      entity_id: `ann-${index + 1}`,
      operation: 'record',
      payload: {},
      created_at: '',
    }));

    let call = 0;
    const result = await syncMapWatermark(
      0,
      101,
      { entities: [baseEntity('1', 'a.py')], annotations: [] },
      {
        fetchChanges: async (afterSeq, limit) => {
          call += 1;
          const slice = events.filter((event) => event.change_seq > afterSeq).slice(0, limit);
          return {
            items: slice,
            last_seq: slice.length ? slice[slice.length - 1].change_seq : afterSeq,
          };
        },
        fetchPathSlice: async () => ({ path: '', entities: [], relations: [], blind_spots: [] }),
      },
    );

    expect(call).toBe(2);
    expect(result.watermark).toBe(101);
    expect(result.incremental).toBe(true);
    expect(result.invalidateAnnotations).toBe(true);
  });

  it('restores entities from path slice beyond first page sort order', async () => {
    const lateEntity = baseEntity('late', 'z.py');
    const result = await syncMapWatermark(
      0,
      1,
      { entities: [baseEntity('keep', 'a.py')], annotations: [] },
      {
        fetchChanges: async () => ({
          items: [{
            change_seq: 1,
            entity_type: 'code_entity',
            entity_id: 'file-z',
            operation: 'refresh',
            payload: { path: 'z.py' },
            created_at: '',
          }],
          last_seq: 1,
        }),
        fetchPathSlice: async () => ({
          path: 'z.py',
          entities: [lateEntity],
          relations: [],
          blind_spots: [],
        }),
      },
    );

    expect(result.cache.entities.map((entity) => entity.id)).toContain('late');
    expect(result.invalidateRelations).toBe(true);
    expect(result.invalidateBlindSpots).toBe(true);
    expect(result.invalidateBoundaries).toBe(true);
  });
});
