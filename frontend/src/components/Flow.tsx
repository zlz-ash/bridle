import { useEffect, useRef } from 'react';
import { DoorIcon, FitIcon, ZoomIn, ZoomOut } from './Icons';
import type { Layout, LaidOutNode } from '../layout/computeLayout';

interface Counts {
  total: number;
  completed: number;
  running: number;
  blocked: number;
}

interface Props {
  layout: Layout;
  selected: string | null;
  onSelect: (id: string | null) => void;
  onExpand?: (id: string) => void;
  zoom: number;
  setZoom: (z: number) => void;
  pan: { x: number; y: number };
  setPan: (p: { x: number; y: number }) => void;
  drawerOpen: boolean;
  toggleDrawer: () => void;
  showGrid: boolean;
  counts: Counts;
  sessionId: string | null;
  sessionStatus: string | null;
  mapView?: 'plan' | 'semantic';
  layerCounts?: { codeEntities: number; blindSpots: number; debt: number };
}

const STATUS_LABEL: Record<string, string> = {
  completed: 'completed', running: 'running', ready: 'ready',
  blocked: 'blocked', pending: 'pending', failed: 'failed', cancelled: 'cancelled',
  drifted: 'drifted', mapping: 'mapping', verifying: 'verifying', debt: 'debt', test: 'test',
};

/** Compute card anchor points; rectangle input exits as SVG routing coordinates. */
function anchors(n: { x: number; y: number; w: number; h: number }) {
  return {
    l: { x: n.x, y: n.y + n.h / 2 },
    r: { x: n.x + n.w, y: n.y + n.h / 2 },
    t: { x: n.x + n.w / 2, y: n.y },
    b: { x: n.x + n.w / 2, y: n.y + n.h },
    cx: n.x + n.w / 2,
    cy: n.y + n.h / 2,
  };
}

/** Build one dependency edge path; edge/map input exits as an SVG path or null when endpoints are hidden. */
function edgePath(
  e: { from: string; to: string; kind: 'flow' },
  byId: Map<string, LaidOutNode>,
): string | null {
  const a = byId.get(e.from);
  const b = byId.get(e.to);
  if (!a || !b) return null;
  const A = anchors(a);
  const B = anchors(b);
  if (Math.abs(A.cy - B.cy) < 2) {
    if (B.cx > A.cx) return `M ${A.r.x} ${A.r.y} H ${B.l.x}`;
    return `M ${A.l.x} ${A.l.y} H ${B.r.x}`;
  }
  // L-shaped vertical drop with horizontal joint
  const midY = (A.cy + B.cy) / 2;
  if (B.cy > A.cy) {
    return `M ${A.b.x} ${A.b.y} V ${midY} H ${B.t.x} V ${B.t.y}`;
  }
  return `M ${A.t.x} ${A.t.y} V ${midY} H ${B.b.x} V ${B.b.y}`;
}

/** Render one map card; laid-out node input exits as a selectable and expandable node view. */
function NodeCard({
  n, selected, onSelect, onExpand,
}: { n: LaidOutNode; selected: boolean; onSelect: (id: string) => void; onExpand?: (id: string) => void }) {
  const style: React.CSSProperties = { left: n.x, top: n.y, width: n.w, height: n.h };
  const extraClass = n.type === 'test' ? ' test' : n.type === 'debt' ? ' debt' : '';
  const showWarn = n.status === 'blocked' || n.status === 'drifted' || n.type === 'debt';
  return (
    <div
      className={'node' + extraClass + (selected ? ' sel' : '')}
      data-st={n.status}
      style={style}
      onClick={(e) => { e.stopPropagation(); onSelect(n.id); }}
    >
      <div className="accent" />
      <div className="nbody">
        <div className="nhead">
          <span className="ntype">{n.type}</span>
          {onExpand ? (
            <button
              type="button"
              aria-label={`Expand ${n.title}`}
              title="Load child nodes"
              onClick={(event) => { event.stopPropagation(); onExpand(n.id); }}
              style={{ marginLeft: 'auto', border: 0, background: 'transparent', cursor: 'pointer' }}
            >
              +
            </button>
          ) : null}
          {n.status === 'blocked' && <span className="warn">⚠</span>}
          {n.status === 'drifted' && <span className="warn" title="Drifted from plan">⚠</span>}
          {n.type === 'debt' && <span className="warn" title="Entangled debt">⚠</span>}
        </div>
        <div className="ntitle">{n.title}</div>
        <div className="nfoot">
          <span className="stat-dot" />
          <span className="stat-label">{STATUS_LABEL[n.status] || n.status}</span>
        </div>
      </div>
    </div>
  );
}

/** Render the zoomable plan map; layout input exits as the existing canvas interaction surface. */
export function Flow(props: Props) {
  const {
    layout, selected, onSelect, zoom, setZoom, pan, setPan,
    drawerOpen, toggleDrawer, showGrid, counts, sessionId, sessionStatus, onExpand,
    mapView = 'plan', layerCounts,
  } = props;

  const wrapRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef(zoom); zoomRef.current = zoom;
  const panRef = useRef(pan); panRef.current = pan;
  const selRef = useRef(onSelect); selRef.current = onSelect;
  const drag = useRef<{ sx: number; sy: number; px: number; py: number; moved: boolean } | null>(null);
  const clamp = (z: number) => Math.min(1.8, Math.max(0.4, z));

  useEffect(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = wrap.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      const z = zoomRef.current;
      const nz = clamp(+(z * (e.deltaY < 0 ? 1.08 : 0.926)).toFixed(4));
      if (nz === z) return;
      const k = nz / z;
      const pn = panRef.current;
      setZoom(nz);
      setPan({ x: mx - (mx - pn.x) * k, y: my - (my - pn.y) * k });
    };

    const onDown = (e: MouseEvent) => {
      if (e.button !== 0) return;
      if ((e.target as HTMLElement).closest('.node')) return;
      drag.current = {
        sx: e.clientX, sy: e.clientY,
        px: panRef.current.x, py: panRef.current.y,
        moved: false,
      };
      wrap.classList.add('grabbing');
    };
    const onMove = (e: MouseEvent) => {
      const d = drag.current;
      if (!d) return;
      const dx = e.clientX - d.sx;
      const dy = e.clientY - d.sy;
      if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
      setPan({ x: d.px + dx, y: d.py + dy });
    };
    const onUp = () => {
      const d = drag.current;
      if (!d) return;
      if (!d.moved) selRef.current(null);
      drag.current = null;
      wrap.classList.remove('grabbing');
    };

    wrap.addEventListener('wheel', onWheel, { passive: false });
    wrap.addEventListener('mousedown', onDown);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      wrap.removeEventListener('wheel', onWheel);
      wrap.removeEventListener('mousedown', onDown);
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [setZoom, setPan]);

  /** Reset the viewport; no input exits with default zoom and pan. */
  const fit = () => { setZoom(1); setPan({ x: 0, y: 0 }); };

  const byId = new Map(layout.nodes.map((n) => [n.id, n]));

  // svg bounds
  const maxX = layout.nodes.reduce((m, n) => Math.max(m, n.x + n.w), 600) + 60;
  const maxY = layout.nodes.reduce((m, n) => Math.max(m, n.y + n.h), 400) + 80;

  return (
    <section className="graph">
      <div className="graph-head">
        <div className="proj">
          <div className="meta">
            {sessionId && (
              <>
                <span>session {sessionId.slice(0, 4)}</span>
                <span className="sep">·</span>
                <span style={{ color: sessionStatus === 'active' ? 'var(--green)' : 'var(--muted)' }}>
                  {sessionStatus || '-'}
                </span>
                <span className="sep">·</span>
              </>
            )}
            <span><b>{counts.total}</b> {mapView === 'plan' ? 'nodes' : 'entities'}</span>
            {mapView === 'plan' ? (
              <>
                <span className="sep">·</span>
                <span><b>{counts.completed}</b> done</span>
                <span className="sep">·</span>
                <span><b>{counts.running}</b> running</span>
                <span className="sep">·</span>
                <span><b>{counts.blocked}</b> blocked</span>
              </>
            ) : layerCounts ? (
              <>
                <span className="sep">·</span>
                <span><b>{layerCounts.codeEntities}</b> code</span>
                <span className="sep">·</span>
                <span><b>{layerCounts.blindSpots}</b> blind</span>
                <span className="sep">·</span>
                <span><b>{layerCounts.debt}</b> debt</span>
              </>
            ) : null}
          </div>
        </div>
        <div className="toolbar" onClick={(e) => e.stopPropagation()}>
          <div className="tool-group">
            <button className="icon-btn" title="Zoom out"
                    onClick={() => setZoom(clamp(+(zoom - 0.1).toFixed(2)))}>
              <ZoomOut />
            </button>
            <button className="icon-btn" title="Reset view" onClick={fit}>
              <FitIcon />
            </button>
            <button className="icon-btn" title="Zoom in"
                    onClick={() => setZoom(clamp(+(zoom + 0.1).toFixed(2)))}>
              <ZoomIn />
            </button>
          </div>
          <button
            className={'door-btn' + (drawerOpen ? ' active' : '')}
            title="Toggle inspector drawer"
            onClick={toggleDrawer}
          >
            <DoorIcon open={drawerOpen} />
          </button>
        </div>
      </div>

      <div
        className={'diagram-wrap' + (showGrid ? ' grid' : '')}
        ref={wrapRef}
        style={showGrid ? {
          backgroundPosition: `${pan.x}px ${pan.y}px`,
          backgroundSize: `${26 * zoom}px ${26 * zoom}px`,
        } : undefined}
      >
        <div className="zoom-badge mono">{Math.round(zoom * 100)}%</div>
        <div
          className="diagram"
          style={{ transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})` }}
        >
          <svg className="edges" width={maxX} height={maxY}>
            <defs>
              <marker id="arrow" markerWidth="9" markerHeight="9" refX="7" refY="4.5"
                      orient="auto" markerUnits="userSpaceOnUse">
                <path d="M1,1 L7,4.5 L1,8" fill="none" stroke="var(--line-3)"
                      strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" />
              </marker>
              <marker id="arrow-blue" markerWidth="9" markerHeight="9" refX="7" refY="4.5"
                      orient="auto" markerUnits="userSpaceOnUse">
                <path d="M1,1 L7,4.5 L1,8" fill="none" stroke="var(--blue-2)"
                      strokeWidth="1.1" strokeLinecap="round" strokeLinejoin="round" />
              </marker>
            </defs>
            {layout.edges.map((e, i) => {
              const d = edgePath(e, byId);
              if (!d) return null;
              const blue = e.live;
              const cls = 'edge-line edge-flow' + (e.live ? ' live' : '');
              return (
                <path
                  key={i}
                  className={cls}
                  d={d}
                  markerEnd={blue ? 'url(#arrow-blue)' : 'url(#arrow)'}
                />
              );
            })}
          </svg>

          {layout.nodes.map((n) => (
            <NodeCard key={n.id} n={n} selected={selected === n.id} onSelect={onSelect} onExpand={onExpand} />
          ))}
        </div>
      </div>
    </section>
  );
}
