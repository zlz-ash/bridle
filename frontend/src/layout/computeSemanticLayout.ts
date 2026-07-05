import type { CodeEntity, CodeRelation, SemanticAnnotation } from '../api/types';
import type { Layout, LaidOutEdge, LaidOutNode } from './computeLayout';
import { MAX_RENDER_NODES } from '../hooks/projectMapPaging';

const W = 158;
const H = 72;
const COL_GAP = 60;
const ROW_GAP = 116;
const X0 = 30;
const Y0 = 62;
const COLS = 4;

function activeAnnotationBySource(annotations: SemanticAnnotation[]): Map<string, SemanticAnnotation> {
  const map = new Map<string, SemanticAnnotation>();
  for (const annotation of annotations) {
    if (annotation.status !== 'active') continue;
    const existing = map.get(annotation.source_id);
    if (!existing || annotation.confidence > existing.confidence) {
      map.set(annotation.source_id, annotation);
    }
  }
  return map;
}

export type SemanticLayoutResult = Layout & {
  renderTruncated: boolean;
  renderedCount: number;
  totalEntityCount: number;
};

/** Lay out code entities, relations, annotations, and debt hints for the semantic map canvas. */
export function computeSemanticLayout(
  entities: CodeEntity[],
  debtNodes: string[],
  relations: CodeRelation[] = [],
  annotations: SemanticAnnotation[] = [],
  blindSpotEntityIds: Set<string> = new Set(),
  renderLimit: number = MAX_RENDER_NODES,
): SemanticLayoutResult {
  const nodes: LaidOutNode[] = [];
  const annotationMap = activeAnnotationBySource(annotations);
  const boundedLimit = Math.max(1, renderLimit);
  const renderEntities = entities.slice(0, boundedLimit);

  for (const [index, entity] of renderEntities.entries()) {
    const row = Math.floor(index / COLS);
    const col = row % 2 === 0 ? index % COLS : COLS - 1 - (index % COLS);
    const isTest = entity.kind === 'test';
    const annotation = annotationMap.get(entity.id);
    const hasBlindSpot = blindSpotEntityIds.has(entity.id);
    const titleSuffix = annotation
      ? ` (${Math.round(annotation.confidence * 100)}%)`
      : hasBlindSpot
        ? ' ⚠'
        : '';
    nodes.push({
      id: entity.id,
      plan_node_id: entity.id,
      title: (entity.path.includes('::') ? entity.path.split('::').pop() ?? entity.name : entity.name) + titleSuffix,
      type: isTest ? 'test' : hasBlindSpot ? 'debt' : entity.kind,
      status: isTest ? 'test' : annotation ? 'mapping' : entity.kind,
      x: X0 + col * (W + COL_GAP),
      y: Y0 + row * (H + ROW_GAP),
      w: W,
      h: H,
      raw: {
        id: entity.id,
        title: entity.path,
        node_type: entity.kind,
        order: index,
        depends_on: [],
        status: isTest ? 'pending' : annotation ? 'mapping' : 'ready',
      },
    });
  }

  const offset = nodes.length;
  for (const [index, debtId] of debtNodes.entries()) {
    if (nodes.length >= boundedLimit) break;
    const slot = offset + index;
    const row = Math.floor(slot / COLS);
    const col = row % 2 === 0 ? slot % COLS : COLS - 1 - (slot % COLS);
    const label = debtId.replace(/^debt:/, '').replace(':', ' ↔ ');
    nodes.push({
      id: debtId,
      plan_node_id: debtId,
      title: `⚠️ 债务 ${label}`,
      type: 'debt',
      status: 'debt',
      x: X0 + col * (W + COL_GAP),
      y: Y0 + row * (H + ROW_GAP),
      w: W,
      h: H,
      raw: {
        id: debtId,
        title: label,
        node_type: 'debt',
        order: slot,
        depends_on: [],
        status: 'blocked',
      },
    });
  }

  const nodeIds = new Set(nodes.map((node) => node.id));
  const edges: LaidOutEdge[] = relations
    .filter(
      (relation) =>
        relation.kind !== 'contains'
        && nodeIds.has(relation.source_id)
        && nodeIds.has(relation.target_id),
    )
    .map((relation) => ({
      from: relation.source_id,
      to: relation.target_id,
      kind: 'flow' as const,
      live: relation.kind === 'calls' || relation.kind === 'imports',
    }));

  return {
    nodes,
    edges,
    lane: null,
    renderTruncated: entities.length > boundedLimit,
    renderedCount: nodes.length,
    totalEntityCount: entities.length,
  };
}
