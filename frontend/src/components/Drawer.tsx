import { useMemo, useState, useEffect } from 'react';
import { DoorIcon, FileGlyph, RerunIcon, StopIcon } from './Icons';
import type { NodeRead, NodeAgentRun } from '../api/types';

type Tab = 'Node Info' | 'Files' | 'Terminal';

interface Props {
  open: boolean;
  toggle: () => void;
  tab: Tab;
  setTab: (t: Tab) => void;
  node: NodeRead | null;
  latestRun: NodeAgentRun | null;
  onRerun: (nodeId: string) => void;
  onCancel: (runId: string) => void;
}

export function Drawer(props: Props) {
  const { open, toggle, tab, setTab, node, latestRun, onRerun, onCancel } = props;
  if (!open) return null;
  const tabs: Tab[] = ['Node Info', 'Files', 'Terminal'];
  return (
    <aside className="drawer open">
      <div className="dwr">
        <div className="dwr-tabs">
          {tabs.map((t) => (
            <button
              key={t}
              className={'dwr-tab' + (tab === t ? ' active' : '')}
              onClick={() => setTab(t)}
            >
              {t}
            </button>
          ))}
          <button className="dwr-close" title="Close inspector" onClick={toggle}>
            <DoorIcon open />
          </button>
        </div>
        <div className="dwr-body">
          {!node ? (
            <div className="empty">Select a node to inspect its run, files, and terminal.</div>
          ) : tab === 'Node Info' ? (
            <NodeInfoTab node={node} run={latestRun} onRerun={onRerun} onCancel={onCancel} />
          ) : tab === 'Files' ? (
            <FilesTab node={node} />
          ) : (
            <TerminalTab node={node} run={latestRun} />
          )}
        </div>
      </div>
    </aside>
  );
}

function formatDuration(ms: number | null): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatHeartbeat(iso: string | null): string {
  if (!iso) return '—';
  const dt = new Date(iso).getTime();
  if (Number.isNaN(dt)) return iso;
  const dx = Math.max(0, Math.round((Date.now() - dt) / 1000));
  if (dx < 60) return `${dx}s ago`;
  if (dx < 3600) return `${Math.round(dx / 60)}m ago`;
  return `${Math.round(dx / 3600)}h ago`;
}

function NodeInfoTab({
  node, run, onRerun, onCancel,
}: {
  node: NodeRead;
  run: NodeAgentRun | null;
  onRerun: (id: string) => void;
  onCancel: (runId: string) => void;
}) {
  const isRunning = node.status === 'running';
  const phase = run?.phase || '—';
  const duration = formatDuration(run?.duration_ms ?? null);
  const attempt = run?.attempt ?? 0;
  const heartbeat = formatHeartbeat(run?.last_heartbeat_at ?? null);
  const metrics = useMemo(() => {
    const out: { k: string; v: string; isJson: boolean }[] = [];
    const m = node.metrics || {};
    for (const [k, v] of Object.entries(m).slice(0, 4)) {
      const isObj = v !== null && typeof v === 'object';
      out.push({
        k,
        v: isObj ? JSON.stringify(v, null, 2) : String(v),
        isJson: isObj,
      });
    }
    if (out.length === 0 && run) {
      out.push(
        { k: 'phase', v: run.phase || '—', isJson: false },
        { k: 'attempt', v: String(run.attempt ?? 0), isJson: false },
        { k: 'duration', v: duration, isJson: false },
        { k: 'status', v: run.status, isJson: false },
      );
    }
    return out;
  }, [node.metrics, run, duration]);

  return (
    <div>
      <div className="ni-head">
        <div className="ni-titlerow">
          <div>
            <div className="ni-title serif">{node.title}</div>
            <div className="ni-id">#{node.plan_node_id}</div>
          </div>
          <div className="ni-actions">
            <button className="mini-btn" title="Re-run node" onClick={() => onRerun(node.id)}>
              <RerunIcon /> rerun
            </button>
            <button
              className="mini-btn danger"
              title="Cancel run"
              disabled={!isRunning || !run}
              onClick={() => run && onCancel(run.run_id)}
            >
              <StopIcon /> cancel
            </button>
          </div>
        </div>
        <span className="pill" data-st={node.status}>
          <span className="pd" />{node.status} · {phase}
        </span>
        <div className="ni-runmeta">
          duration {duration} · attempt {attempt} · heartbeat {heartbeat}
        </div>
      </div>

      <div className="sec">
        <div className="sec-label">Purpose</div>
        <p>{node.goal || '—'}</p>
      </div>

      {run?.blocked_reason && (
        <div className="sec">
          <div className="sec-label">Blocked reason</div>
          <p style={{ color: 'var(--red)' }}>{run.blocked_reason}</p>
        </div>
      )}

      {metrics.length > 0 && (
        <div className="sec">
          <div className="sec-label">Metrics</div>
          <div className="metrics">
            {metrics.map((m, i) => (
              <div className={'metric-cell' + (m.isJson ? ' json' : '')} key={i}>
                <div className="k">{m.k}</div>
                {m.isJson ? <pre className="v-json">{m.v}</pre> : <div className="v">{m.v}</div>}
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="sec">
        <div className="sec-label">Latest summary</div>
        {run ? (
          <>
            <div className="logline">
              <span className="lt">res</span>
              <span className="lv">{run.result_summary || '—'}</span>
            </div>
            {run.test_summary && (
              <div className="logline">
                <span className="lt">tst</span>
                <span className="lv ok">{run.test_summary}</span>
              </div>
            )}
            {run.metrics_summary && (
              <div className="logline">
                <span className="lt">met</span>
                <span className="lv">{run.metrics_summary}</span>
              </div>
            )}
            {run.error_code && (
              <div className="logline">
                <span className="lt">err</span>
                <span className="lv warn">{run.error_code}</span>
              </div>
            )}
          </>
        ) : (
          <p style={{ color: 'var(--muted)' }}>No run recorded for this node yet.</p>
        )}
      </div>
    </div>
  );
}

function FilesTab({ node }: { node: NodeRead }) {
  const groups = [
    { label: 'write set', items: node.write_set, tag: 'rw' },
    { label: 'read set', items: node.read_set, tag: 'r' },
    { label: 'readonly context', items: node.readonly_context, tag: 'ctx' },
    { label: 'tests', items: node.tests, tag: 'test' },
  ].filter((g) => g.items && g.items.length);

  const allFiles = groups.flatMap((g) => g.items);
  const [active, setActive] = useState<string | null>(allFiles[0] || null);
  useEffect(() => { setActive(allFiles[0] || null); }, [node.id]);

  return (
    <div>
      {groups.length === 0 && (
        <div className="empty">No files declared for this node.</div>
      )}
      {groups.map((g, gi) => (
        <div key={gi}>
          <div className="file-group-label">{g.label}</div>
          {g.items.map((f, i) => (
            <div
              key={i}
              className={'file-row' + (active === f ? ' active' : '')}
              onClick={() => setActive(f)}
            >
              <span className="ficon"><FileGlyph /></span>
              <span>{f}</span>
              <span className="ftag">{g.tag}</span>
            </div>
          ))}
        </div>
      ))}
      {active && (
        <div className="code-prev">
          <div className="cp-head">
            <span>{active}</span>
            <span>preview not wired</span>
          </div>
          <pre>
            <div>
              <span className="ln">1</span>
              <span className="cp-com"># File preview endpoint not implemented yet.</span>
            </div>
            <div>
              <span className="ln">2</span>
              <span className="cp-com"># See Frontend-Design.md §5.2 / §11 — workspace files API.</span>
            </div>
          </pre>
        </div>
      )}
    </div>
  );
}

function TerminalTab({ node, run }: { node: NodeRead; run: NodeAgentRun | null }) {
  const lines = (run?.container_logs_summary || '').split('\n').filter(Boolean);
  return (
    <div>
      <div className="term">
        <div className="t-head">
          <span className="tdot" />container · {node.plan_node_id} · run {run?.attempt ?? '—'}
        </div>
        <pre>
          {lines.length === 0 ? (
            <div><span className="dim">no container logs</span></div>
          ) : (
            lines.map((line, i) => <div key={i}>{line}</div>)
          )}
        </pre>
      </div>
      <div className="term-foot" title="diagnostic trace path">
        full log → {run?.diagnostic_path || '—'}
      </div>
    </div>
  );
}
