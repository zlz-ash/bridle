import type { NodeRead } from '../api/types';

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
  isTest: boolean;
  raw: NodeRead;
}

export interface LaidOutEdge {
  from: string;
  to: string;
  kind: 'flow' | 'test';
  live?: boolean;
}

export interface Layout {
  nodes: LaidOutNode[];
  edges: LaidOutEdge[];
  lane: { x: number; y: number; w: number; h: number; count: number } | null;
}

/**
 * Serpentine layout (matches design): main nodes flow left→right in alternating
 * rows; test-typed nodes get their own swimlane at the bottom.
 * Card sizes auto-grow vertically based on text length (min 72 / 84).
 */
/** Word-aware line counter: ASCII runs are atomic, CJK chars wrap individually. */
function estimateLines(text: string, charsPerLine: number): number {
  if (!text) return 0;
  // Build "units": each CJK char is a unit width 1; each ASCII run is a unit width 0.5*len.
  const units: number[] = [];
  let buf = '';
  const flush = () => {
    if (buf) { units.push(buf.length * 0.5); buf = ''; }
  };
  for (const ch of text) {
    const code = ch.charCodeAt(0);
    if (code > 127) { flush(); units.push(1); }
    else if (/\s/.test(ch)) { flush(); }
    else { buf += ch; }
  }
  flush();
  if (!units.length) return 0;
  // Greedy pack onto lines; a single unit larger than charsPerLine occupies its own line.
  let lines = 1;
  let used = 0;
  for (const u of units) {
    if (used === 0) { used = u; continue; }
    if (used + u > charsPerLine) { lines++; used = u; }
    else { used += u; }
  }
  return lines;
}

function estimateHeight(opts: {
  base: number;          // baseline chrome (header + padding + footer)
  titleSize: number;     // chars per title line
  titleText: string;
  titleLineHeight?: number;
  bodySize?: number;     // chars per body line
  bodyText?: string;
  bodyLineHeight?: number;
  minHeight: number;
}): number {
  const titleLines = Math.max(1, estimateLines(opts.titleText, opts.titleSize));
  let h = opts.base + titleLines * (opts.titleLineHeight ?? 20);
  if (opts.bodyText && opts.bodySize) {
    const bodyLines = estimateLines(opts.bodyText, opts.bodySize);
    if (bodyLines > 0) h += bodyLines * (opts.bodyLineHeight ?? 18) + 6;
  }
  return Math.max(opts.minHeight, h);
}

export function computeLayout(allNodes: NodeRead[]): Layout {
  const W_MAIN = 158;
  const H_MAIN = 72;
  const W_TEST = 196;
  const H_TEST = 84;
  const COL_GAP = 60;
  const ROW_GAP = 116;
  const X0 = 30;
  const Y0 = 62;
  const COLS = 4;

  const isTestNode = (n: NodeRead) => (n.node_type || '').toLowerCase().includes('test');

  const mains = allNodes.filter((n) => !isTestNode(n)).sort((a, b) => a.order - b.order);
  const tests = allNodes.filter(isTestNode).sort((a, b) => a.order - b.order);

  // Serpentine row layout for main nodes.
  // Per-row max height computed in second pass so siblings align.
  // Main card chrome: accent(3) + pad(9+10) + nhead(12) + gap(5) + gap(5) + nfoot(10) ≈ 54
  // Title font 15px serif; ~9 wide-chars per line at 134px content width.
  const mainHeights = mains.map((n) =>
    estimateHeight({
      base: 54,
      titleSize: 9,
      titleText: n.title,
      titleLineHeight: 22,
      minHeight: H_MAIN,
    }),
  );
  const rowMaxH: number[] = [];
  for (let i = 0; i < mainHeights.length; i++) {
    const r = Math.floor(i / COLS);
    rowMaxH[r] = Math.max(rowMaxH[r] || 0, mainHeights[i]);
  }
  const rowYStart: number[] = [];
  let cursorY = Y0;
  for (let r = 0; r < rowMaxH.length; r++) {
    rowYStart[r] = cursorY;
    cursorY += rowMaxH[r] + ROW_GAP;
  }
  const laidMains: LaidOutNode[] = mains.map((n, i) => {
    const row = Math.floor(i / COLS);
    const col = row % 2 === 0 ? i % COLS : COLS - 1 - (i % COLS);
    const x = X0 + col * (W_MAIN + COL_GAP);
    const y = rowYStart[row];
    return {
      id: n.id,
      plan_node_id: n.plan_node_id,
      title: n.title,
      type: n.node_type || 'node',
      status: n.status,
      x,
      y,
      w: W_MAIN,
      h: rowMaxH[row],
      isTest: false,
      raw: n,
    };
  });

  const lastMainY = laidMains.length
    ? Math.max(...laidMains.map((n) => n.y + n.h))
    : Y0 + H_MAIN;
  const laneY = lastMainY + 80;

  const laidTests: LaidOutNode[] = tests.map((n, i) => {
    const x = X0 + 170 + i * (W_TEST + 24);
    // Test card chrome: accent(3) + pad(9+10) + nhead(12) + gap(5) + gap(5) ≈ 44
    // 196px width → ~12 wide-chars for title, ~13 for body
    const h = estimateHeight({
      base: 44,
      titleSize: 12,
      titleText: n.title,
      titleLineHeight: 22,
      bodySize: 13,
      bodyText: n.goal || '',
      bodyLineHeight: 18,
      minHeight: H_TEST,
    });
    return {
      id: n.id,
      plan_node_id: n.plan_node_id,
      title: n.title,
      type: 'test',
      status: n.status,
      x,
      y: laneY + 30,
      w: W_TEST,
      h,
      isTest: true,
      raw: n,
    };
  });

  const laneRight = laidTests.length
    ? Math.max(...laidTests.map((n) => n.x + n.w)) + 40
    : X0 + W_MAIN * COLS;
  const maxTestH = laidTests.length ? Math.max(...laidTests.map((t) => t.h)) : H_TEST;
  const lane = laidTests.length
    ? { x: X0 + 140, y: laneY, w: laneRight - (X0 + 140), h: maxTestH + 70, count: laidTests.length }
    : null;

  // Edges: from depends_on. Live = source running, target pending/ready.
  const allLaid = [...laidMains, ...laidTests];
  const byPlanNodeId = new Map(allLaid.map((n) => [n.plan_node_id, n]));
  const edges: LaidOutEdge[] = [];
  for (const n of allLaid) {
    for (const dep of n.raw.depends_on) {
      const src = byPlanNodeId.get(dep);
      if (!src) continue;
      const kind: 'flow' | 'test' = n.isTest ? 'test' : 'flow';
      const live = src.status === 'running' && (n.status === 'pending' || n.status === 'ready');
      edges.push({ from: src.id, to: n.id, kind, live });
    }
  }

  return { nodes: allLaid, edges, lane };
}
