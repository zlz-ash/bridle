import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as ReactPointerEvent } from 'react';
import { Badge } from '../ds';
import type { BadgeTone } from '../ds';
import { Folder } from '../ds/Icons';
import type { NodeRead, NodeStatus, NodeTestDetail, BlindSpot } from '../../api/types';

export type InspectorTab = 'Node Info' | 'Files' | 'Issues';

export interface RightInspectorProps {
  open: boolean;
  tab: InspectorTab;
  setTab: (next: InspectorTab) => void;
  width: number;
  setWidth: (next: number) => void;
  node: NodeRead | null;
  /** Workspace-scope display when no node is selected. */
  workspaceLabel?: string | null;
  sessionId?: string | null;
  blindSpots?: BlindSpot[];
  pendingArbitrationCount?: number;
}

const MIN_W = 280;
const MAX_W = 720;

const STATUS_TONE: Record<NodeStatus, BadgeTone> = {
  pending: 'idle',
  ready: 'neutral',
  running: 'running',
  blocked: 'failed',
  completed: 'completed',
  failed: 'failed',
  cancelled: 'idle',
  archived: 'idle',
  proposed: 'neutral',
  ratified: 'completed',
  mapping: 'running',
  executing: 'running',
  verifying: 'running',
  drifted: 'failed',
};

const STATUS_LABEL: Record<NodeStatus, string> = {
  pending: 'Pending',
  ready: 'Ready',
  running: 'Running',
  blocked: 'Blocked',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
  archived: 'Archived',
  proposed: 'Proposed',
  ratified: 'Ratified',
  mapping: 'Mapping',
  executing: 'Executing',
  verifying: 'Verifying',
  drifted: 'Drifted',
};

function normalizeTestDetails(node: NodeRead): NodeTestDetail[] {
  if (Array.isArray(node.test_details) && node.test_details.length > 0) {
    return node.test_details;
  }
  return (node.tests || []).map((command) => ({ command, purpose: '' }));
}

function eyebrow(label: string): CSSProperties {
  return {
    fontSize: 11,
    fontWeight: 700,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: 'var(--text-cream-3)',
    marginBottom: 6,
  };
}

function NodeInfoTab({
  node,
}: {
  node: NodeRead;
}) {
  const testDetails = useMemo(() => normalizeTestDetails(node), [node]);
  const testCount = node.test_count ?? testDetails.length;

  const metrics = useMemo(() => {
    const out: { k: string; v: string; isJson: boolean }[] = [];
    const source = node.metrics || {};
    for (const [k, v] of Object.entries(source).slice(0, 4)) {
      const isObject = v !== null && typeof v === 'object';
      out.push({
        k,
        v: isObject ? JSON.stringify(v, null, 2) : String(v),
        isJson: isObject,
      });
    }
    return out;
  }, [node.metrics]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18, fontSize: 13 }}>
      <div>
        <div style={eyebrow('Purpose')}>Purpose</div>
        <div style={{ color: 'var(--text-cream-1)', lineHeight: 1.55 }}>{node.goal || '-'}</div>
      </div>

      <div>
        <div style={eyebrow('Tests')}>Tests - {testCount}</div>
        {testDetails.length === 0 ? (
          <div style={{ color: 'var(--text-cream-3)' }}>No tests declared.</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {testDetails.map((detail, i) => (
              <div
                key={`${detail.command}-${i}`}
                style={{
                  background: 'var(--cream-04)',
                  border: '1px solid var(--hairline)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '8px 10px',
                }}
              >
                <div
                  style={{
                    fontSize: 12,
                    color: 'var(--text-cream-2)',
                    marginBottom: 4,
                  }}
                >
                  {detail.purpose || 'No purpose recorded'}
                </div>
                <div
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 12,
                    color: 'var(--text-cream-1)',
                    wordBreak: 'break-all',
                  }}
                >
                  {detail.command}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {metrics.length > 0 ? (
        <div>
          <div style={eyebrow('Metrics')}>Metrics</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {metrics.map((m, i) => (
              <div
                key={`${m.k}-${i}`}
                style={{
                  background: 'var(--cream-04)',
                  border: '1px solid var(--hairline)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '8px 10px',
                }}
              >
                <div style={{ fontSize: 11, color: 'var(--text-cream-3)', marginBottom: 4 }}>
                  {m.k}
                </div>
                {m.isJson ? (
                  <pre
                    style={{
                      margin: 0,
                      fontFamily: 'var(--font-mono)',
                      fontSize: 12,
                      color: 'var(--text-cream-1)',
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {m.v}
                  </pre>
                ) : (
                  <div style={{ fontFamily: 'var(--font-mono)', fontSize: 12, color: 'var(--text-cream-1)' }}>
                    {m.v}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ) : null}

    </div>
  );
}

function FilesTab({ node }: { node: NodeRead }) {
  const groups = [
    { label: 'write set', items: node.write_set, tag: 'M', tone: 'var(--accent-amber)' },
    { label: 'read set', items: node.read_set, tag: 'R', tone: 'var(--text-cream-2)' },
    { label: 'readonly context', items: node.readonly_context, tag: 'ctx', tone: 'var(--text-cream-3)' },
  ].filter((group) => group.items && group.items.length);

  if (groups.length === 0) {
    return <div style={{ color: 'var(--text-cream-3)' }}>No files declared for this node.</div>;
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {groups.map((group, gi) => (
        <div key={gi}>
          <div style={eyebrow(group.label)}>{group.label}</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
            {group.items.map((file, fi) => (
              <div
                key={`${file}-${fi}`}
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 9,
                  padding: '6px 6px',
                  borderRadius: 'var(--radius-xs)',
                  fontFamily: 'var(--font-mono)',
                  fontSize: 12,
                }}
              >
                <span
                  title={group.label}
                  style={{
                    width: 14,
                    textAlign: 'center',
                    color: group.tone,
                    fontWeight: 600,
                  }}
                >
                  {group.tag}
                </span>
                <span
                  style={{
                    flex: 1,
                    minWidth: 0,
                    color: 'var(--text-cream-1)',
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                  }}
                >
                  {file}
                </span>
              </div>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function TabButton({
  id,
  label,
  active,
  onClick,
}: {
  id: InspectorTab;
  label: string;
  active: boolean;
  onClick: (next: InspectorTab) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onClick(id)}
      style={{
        appearance: 'none',
        border: 'none',
        background: 'transparent',
        cursor: 'pointer',
        font: 'inherit',
        fontSize: 13,
        fontWeight: active ? 600 : 500,
        color: active ? 'var(--text-cream-1)' : 'rgba(245,232,208,0.5)',
        padding: '10px 2px',
        borderBottom: active ? '2px solid var(--accent-amber)' : '2px solid transparent',
        marginBottom: -1,
        transition: 'color var(--dur-hover) var(--ease-out)',
      }}
    >
      {label}
    </button>
  );
}

/** List open blind spots and pending arbitration for bootstrap review. */
function IssuesTab({
  blindSpots,
  pendingArbitrationCount,
}: {
  blindSpots: BlindSpot[];
  pendingArbitrationCount: number;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      {pendingArbitrationCount > 0 ? (
        <div style={{ color: 'var(--accent-copper)', fontSize: 13 }}>
          {pendingArbitrationCount} semantic annotation(s) awaiting arbitration
        </div>
      ) : null}
      <div style={eyebrow('')}>Open blind spots</div>
      {blindSpots.length === 0 ? (
        <div style={{ color: 'var(--text-cream-3)', fontSize: 13 }}>No open blind spots.</div>
      ) : (
        blindSpots.map((spot) => (
          <div
            key={spot.id}
            style={{
              border: '1px solid var(--hairline)',
              borderRadius: 8,
              padding: '10px 12px',
              fontSize: 12,
            }}
          >
            <div style={{ fontWeight: 600, color: 'var(--text-cream-1)' }}>{spot.kind}</div>
            <div style={{ color: 'var(--text-cream-3)', marginTop: 4 }}>{spot.file_path ?? '—'}</div>
            <div style={{ color: 'var(--text-cream-2)', marginTop: 4 }}>{spot.source}</div>
          </div>
        ))
      )}
    </div>
  );
}

/** Right inspector - per-node tabs (Node Info / Files),
 *  draggable left-edge resize handle (280-20px, persisted), header chevron toggle. */
export function RightInspector({
  open,
  tab,
  setTab,
  width,
  setWidth,
  node,
  workspaceLabel,
  sessionId,
  blindSpots = [],
  pendingArbitrationCount = 0,
}: RightInspectorProps) {
  const draggingRef = useRef(false);
  const [hoverHandle, setHoverHandle] = useState(false);
  const tone: BadgeTone = node ? STATUS_TONE[node.status] : 'idle';
  const label = node ? STATUS_LABEL[node.status] : '';

  const handleDown = (e: ReactPointerEvent) => {
    e.preventDefault();
    draggingRef.current = true;
    const startX = e.clientX;
    const startW = width;
    const onMove = (ev: PointerEvent) => {
      if (!draggingRef.current) return;
      setWidth(Math.max(MIN_W, Math.min(MAX_W, startW + (startX - ev.clientX))));
    };
    const onUp = () => {
      draggingRef.current = false;
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
  };

  if (!open) return null;

  return (
    <aside
      style={{
        position: 'relative',
        width,
        flex: 'none',
        display: 'flex',
        background: 'var(--panel)',
        borderLeft: '1px solid var(--hairline)',
        minWidth: 0,
        height: '100%',
      }}
    >
      <div
        onPointerDown={handleDown}
        onMouseEnter={() => setHoverHandle(true)}
        onMouseLeave={() => setHoverHandle(false)}
        title="Drag to resize"
        style={{
          width: 6,
          flex: 'none',
          cursor: 'col-resize',
          background: hoverHandle ? 'var(--amber-24)' : 'transparent',
          transition: 'background var(--dur-hover) var(--ease-out)',
        }}
      />

      <div
        style={{
          flex: 1,
          minWidth: 0,
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        {/* Topmost layer -tabs only. Node identity moved into Node Info content. */}
        <div
          style={{
            display: 'flex',
            gap: 18,
            padding: '0 16px',
            borderBottom: '1px solid var(--hairline)',
            flex: 'none',
          }}
        >
          <TabButton id="Node Info" label="Node Info" active={tab === 'Node Info'} onClick={setTab} />
          <TabButton id="Files" label="Files" active={tab === 'Files'} onClick={setTab} />
          <TabButton id="Issues" label="Issues" active={tab === 'Issues'} onClick={setTab} />
        </div>

        {/* Tab content */}
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: 'auto',
            padding: 16,
          }}
        >
          {!node ? (
            tab === 'Issues' ? (
              <IssuesTab blindSpots={blindSpots} pendingArbitrationCount={pendingArbitrationCount} />
            ) : tab === 'Node Info' ? (
              <div
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  gap: 8,
                  color: 'var(--text-cream-3)',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 10,
                    padding: '8px 0 14px',
                    borderBottom: '1px solid var(--hairline)',
                  }}
                >
                  <span style={{ display: 'flex', color: 'var(--text-cream-2)' }}>
                    <Folder size={17} />
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div
                      style={{
                        fontSize: 14,
                        fontWeight: 600,
                        color: 'var(--text-cream-1)',
                      }}
                    >
                      {workspaceLabel ?? 'Workspace'}
                    </div>
                    {sessionId ? (
                      <div
                        style={{
                          fontFamily: 'var(--font-mono)',
                          fontSize: 11,
                          color: 'var(--text-cream-3)',
                        }}
                      >
                        session {sessionId}
                      </div>
                    ) : null}
                  </div>
                </div>
                <div style={{ paddingTop: 8 }}>
                  Select a node in the flowchart to inspect its run details.
                </div>
              </div>
            ) : (
              <div style={{ color: 'var(--text-cream-3)' }}>
                Select a node to inspect its files.
              </div>
            )
          ) : tab === 'Node Info' ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
              {/* node identity -first block inside Node Info, not a global header */}
              <div
                style={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 10,
                  paddingBottom: 14,
                  borderBottom: '1px solid var(--hairline)',
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontSize: 15,
                      fontWeight: 600,
                      color: 'var(--text-cream-1)',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    <span style={{ color: 'var(--text-cream-3)', fontWeight: 500 }}>unit: </span>
                    {node.title}
                  </div>
                  <div
                    style={{
                      fontFamily: 'var(--font-mono)',
                      fontSize: 11,
                      color: 'var(--text-cream-3)',
                    }}
                  >
                    #{node.plan_node_id}
                  </div>
                </div>
                <Badge tone={tone} dot>
                  {label}
                </Badge>
              </div>
              <NodeInfoTab node={node} />
            </div>
          ) : tab === 'Files' ? (
            <FilesTab node={node} />
          ) : (
            <IssuesTab blindSpots={blindSpots} pendingArbitrationCount={pendingArbitrationCount} />
          )}
        </div>
      </div>
    </aside>
  );
}


