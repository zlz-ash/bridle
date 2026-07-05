import { describe, expect, it } from 'vitest';
import { computeSemanticLayout } from '../computeSemanticLayout';
import { MAX_RENDER_NODES } from '../../hooks/projectMapPaging';
import type { CodeEntity, CodeRelation, SemanticAnnotation } from '../../api/types';

describe('computeSemanticLayout', () => {
  it('renders only active annotations and reports render truncation', () => {
    const entities: CodeEntity[] = Array.from({ length: 401 }, (_, index) => ({
      id: `e-${index}`,
      path: `mod/file_${index}.py`,
      kind: 'file',
      name: `file_${index}.py`,
      parent_id: null,
      payload: {},
    }));
    const annotations: SemanticAnnotation[] = [
      {
        id: 'ann-active',
        source_id: 'e-0',
        summary: 'entrypoint',
        evidence: {},
        model: 'test',
        confidence: 0.92,
        file_hash: 'abc',
        status: 'active',
      },
      {
        id: 'ann-pending',
        source_id: 'e-1',
        summary: 'pending guess',
        evidence: {},
        model: 'test',
        confidence: 0.5,
        file_hash: 'abc',
        status: 'pending',
      },
    ];
    const layout = computeSemanticLayout(entities, [], [], annotations, new Set(), MAX_RENDER_NODES);
    expect(layout.renderTruncated).toBe(true);
    expect(layout.renderedCount).toBe(MAX_RENDER_NODES);
    expect(layout.totalEntityCount).toBe(401);
    expect(layout.nodes.find((node) => node.id === 'e-0')?.title).toContain('92%');
    expect(layout.nodes.find((node) => node.id === 'e-1')?.title).not.toContain('%');
  });

  it('draws relation edges between rendered nodes', () => {
    const entities: CodeEntity[] = [
      {
        id: 'e-a',
        path: 'a.py',
        kind: 'file',
        name: 'a.py',
        parent_id: null,
        payload: {},
      },
      {
        id: 'e-b',
        path: 'b.py',
        kind: 'file',
        name: 'b.py',
        parent_id: null,
        payload: {},
      },
    ];
    const relations: CodeRelation[] = [
      { source_id: 'e-a', target_id: 'e-b', kind: 'imports', payload: {} },
    ];
    const layout = computeSemanticLayout(entities, [], relations, [], new Set(), 400);
    expect(layout.edges).toHaveLength(1);
  });
});
