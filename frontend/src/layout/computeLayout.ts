import type { NodeStatus } from '../api/types';

export interface LayoutNodeInput {
  id: string;
  title: string;
  node_type: string | null;
  order: number;
  depends_on: string[];
  status: NodeStatus;
}

export interface LaidOutNode {
  id: string;
  plan_node_id: string;
  title: string;
  type: string;
  status: string;
  x: number;
  y: number;
  w: number;
  h: number;
  raw: LayoutNodeInput;
}

export interface LaidOutEdge {
  from: string;
  to: string;
  kind: 'flow';
  live?: boolean;
}

export interface Layout {
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
  lane: null;
}

/**
 * Serpentine layout: workflow nodes flow left to right in alternating rows.
 * Tests belong to the workflow node and do not render in a separate lane.
 */
/** Estimate wrapped text lines; text/width input exits as a display-line count. */
function estimateLines(text: string, charsPerLine: number): number {
  if (!text) return 0;
  const units: number[] = [];
  let buf = '';
  const flush = () => {
    if (buf) {
      units.push(buf.length * 0.5);
      buf = '';
    }
  };
  for (const ch of text) {
    const code = ch.charCodeAt(0);
    if (code > 127) {
      flush();
      units.push(1);
    } else if (/\s/.test(ch)) {
      flush();
    } else {
      buf += ch;
    }
  }
  flush();
  if (!units.length) return 0;
  let lines = 1;
  let used = 0;
  for (const unit of units) {
    if (used === 0) {
      used = unit;
      continue;
    }
    if (used + unit > charsPerLine) {
      lines++;
      used = unit;
    } else {
      used += unit;
    }
  }
  return lines;
}

/** Estimate card height; sizing/text input exits as a minimum-bounded pixel height. */
function estimateHeight(opts: {
  base: number;
  titleSize: number;
  titleText: string;
  titleLineHeight?: number;
  bodySize?: number;
  bodyText?: string;
  bodyLineHeight?: number;
  minHeight: number;
}): number {
  const titleLines = Math.max(1, estimateLines(opts.titleText, opts.titleSize));
  let height = opts.base + titleLines * (opts.titleLineHeight ?? 20);
  if (opts.bodyText && opts.bodySize) {
    const bodyLines = estimateLines(opts.bodyText, opts.bodySize);
    if (bodyLines > 0) height += bodyLines * (opts.bodyLineHeight ?? 18) + 6;
  }
  return Math.max(opts.minHeight, height);
}

/** Lay out visible map nodes; node inputs exit as the existing Flow graph contract. */
export function computeLayout(allNodes: LayoutNodeInput[]): Layout {
  const W_MAIN = 158;
  const H_MAIN = 72;
  const COL_GAP = 60;
  const ROW_GAP = 116;
  const X0 = 30;
  const Y0 = 62;
  const COLS = 4;

  const nodes = [...allNodes].sort((a, b) => a.order - b.order);
  const mainHeights = nodes.map((node) =>
    estimateHeight({
      base: 54,
      titleSize: 9,
      titleText: node.title,
      titleLineHeight: 22,
      minHeight: H_MAIN,
    }),
  );

  const rowMaxH: number[] = [];
  for (let i = 0; i < mainHeights.length; i++) {
    const row = Math.floor(i / COLS);
    rowMaxH[row] = Math.max(rowMaxH[row] || 0, mainHeights[i]);
  }

  const rowYStart: number[] = [];
  let cursorY = Y0;
  for (let row = 0; row < rowMaxH.length; row++) {
    rowYStart[row] = cursorY;
    cursorY += rowMaxH[row] + ROW_GAP;
  }

  const laidNodes: LaidOutNode[] = nodes.map((node, index) => {
    const row = Math.floor(index / COLS);
    const col = row % 2 === 0 ? index % COLS : COLS - 1 - (index % COLS);
    return {
      id: node.id,
      plan_node_id: node.id,
      title: node.title,
      type: node.node_type || 'node',
      status: node.status,
      x: X0 + col * (W_MAIN + COL_GAP),
      y: rowYStart[row],
      w: W_MAIN,
      h: rowMaxH[row],
      raw: node,
    };
  });

  const byPlanNodeId = new Map(laidNodes.map((node) => [node.plan_node_id, node]));
  const edges: LaidOutEdge[] = [];
  for (const node of laidNodes) {
    for (const dep of node.raw.depends_on) {
      const src = byPlanNodeId.get(dep);
      if (!src) continue;
      const live = src.status === 'running' && (node.status === 'pending' || node.status === 'ready');
      edges.push({ from: src.id, to: node.id, kind: 'flow', live });
    }
  }

  return { nodes: laidNodes, edges, lane: null };
}
