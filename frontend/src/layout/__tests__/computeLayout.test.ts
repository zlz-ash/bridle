import { describe, expect, it } from 'vitest';
import { computeLayout } from '../computeLayout';
import type { NodeRead } from '../../api/types';

function makeNode(overrides: Partial<NodeRead> = {}): NodeRead {
  return {
    id: overrides.id ?? 'db-node-1',
    plan_id: overrides.plan_id ?? 'plan-1',
    plan_node_id: overrides.plan_node_id ?? 'n1',
    title: overrides.title ?? 'Workflow node',
    goal: overrides.goal ?? 'Goal',
    node_type: overrides.node_type ?? 'code_change',
    order: overrides.order ?? 0,
    depends_on: overrides.depends_on ?? [],
    files: overrides.files ?? ['src/feature.py'],
    tests: overrides.tests ?? ['pytest tests/test_feature.py -q'],
    test_details: overrides.test_details ?? [
      { command: 'pytest tests/test_feature.py -q', purpose: 'verify feature behavior' },
    ],
    test_count: overrides.test_count ?? 1,
    metrics: overrides.metrics ?? {},
    constraints: overrides.constraints ?? {},
    review_checks: overrides.review_checks ?? [],
    expected_outputs: overrides.expected_outputs ?? {},
    interfaces: overrides.interfaces ?? {},
    read_set: overrides.read_set ?? ['src/feature.py'],
    write_set: overrides.write_set ?? ['src/feature.py'],
    readonly_context: overrides.readonly_context ?? [],
    conflict_contributions: overrides.conflict_contributions ?? [],
    status: overrides.status ?? 'pending',
    created_at: overrides.created_at ?? '2026-01-01T00:00:00',
    updated_at: overrides.updated_at ?? '2026-01-01T00:00:00',
  };
}

describe('computeLayout', () => {
  it('does not create a separate lane for test-typed nodes', () => {
    const layout = computeLayout([
      makeNode({ id: 'db-node-1', plan_node_id: 'n1', order: 0, node_type: 'code_change' }),
      makeNode({ id: 'db-node-2', plan_node_id: 'n2', order: 1, node_type: 'test_validation', depends_on: ['n1'] }),
    ]);

    expect(layout.nodes).toHaveLength(2);
    expect(layout.lane).toBeNull();
    expect(layout.edges.every((edge) => edge.kind === 'flow')).toBe(true);
  });
});
