import { useEffect, useMemo, useState } from 'react';
import { Chat } from './components/Chat';
import { Flow } from './components/Flow';
import { Drawer } from './components/Drawer';
import { LogoMark, GearIcon } from './components/Icons';
import { computeLayout } from './layout/computeLayout';
import {
  useFirstTask, useCurrentPlan, useNode, useLatestRun,
  useNodeMutations, useCounts,
} from './hooks/useBridleData';
import { usePlanModeFlow } from './hooks/usePlanModeFlow';
import type { WorkspaceEntry } from './components/WorkspaceSwitcher';

type Tab = 'Node Info' | 'Files' | 'Terminal';

const DEFAULT_WORKSPACES: WorkspaceEntry[] = [
  { id: 'local', name: 'local', path: 'http://127.0.0.1:8900' },
];

function loadWorkspaces(): WorkspaceEntry[] {
  try {
    const raw = localStorage.getItem('bridle.workspaces');
    if (raw) {
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed) && parsed.length) return parsed;
    }
  } catch { /* ignore */ }
  return DEFAULT_WORKSPACES;
}

export function App() {
  const [selected, setSelected] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [tab, setTab] = useState<Tab>('Node Info');
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [chatW, setChatW] = useState(300);
  const [workspaces, setWorkspaces] = useState<WorkspaceEntry[]>(loadWorkspaces);
  const [activeWs, setActiveWs] = useState<string>(() => workspaces[0]?.id || 'local');

  useEffect(() => {
    localStorage.setItem('bridle.workspaces', JSON.stringify(workspaces));
  }, [workspaces]);

  const createWorkspace = ({ name, path }: { name: string; path: string }) => {
    const id = 'ws-' + Date.now();
    setWorkspaces((ws) => [{ id, name, path }, ...ws]);
    setActiveWs(id);
  };

  const { task } = useFirstTask();
  const planQ = useCurrentPlan();
  const plan = planQ.data;
  const flow = usePlanModeFlow(activeWs);

  const nodeQ = useNode(selected);
  const runQ = useLatestRun(selected);
  const { rerun, cancelRun } = useNodeMutations();

  const layout = useMemo(
    () => computeLayout(plan?.nodes || []),
    [plan?.nodes],
  );

  const counts = useCounts(layout.nodes.map((n) => n.status));

  const planNodeIdToId = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of layout.nodes) m.set(n.plan_node_id, n.id);
    return m;
  }, [layout.nodes]);

  const selectNode = (id: string | null) => {
    setSelected(id);
    if (id) {
      setDrawerOpen(true);
      setTab('Node Info');
    }
  };

  const onMention = (planNodeId: string) => {
    const id = planNodeIdToId.get(planNodeId);
    if (id) selectNode(id);
  };

  const showGraph = flow.mode === 'execute' && plan && !planQ.isLoading;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <LogoMark size={22} />
          <span className="wordmark">bridle</span>
          <span className="tag">agent workspace</span>
        </div>
        <div className="spacer" />
        <div className="controls">
          <button className="icon-btn" title="Settings"><GearIcon /></button>
        </div>
      </header>

      <div className="body">
        <Chat
          mode={flow.mode}
          messages={flow.messages}
          onMention={onMention}
          onSend={(t) => void flow.send(t)}
          width={chatW}
          setWidth={setChatW}
          workspaces={workspaces}
          activeWs={activeWs}
          onSwitch={setActiveWs}
          onCreate={createWorkspace}
          canSend={flow.canSend}
          disabledReason={
            flow.mode === 'execute' && !flow.canSend
              ? 'Start a coding session first'
              : undefined
          }
          proposedPlan={flow.proposedPlan}
          parseError={flow.parseError}
          onConfirmPlan={() => void flow.confirmPlan()}
          onDiscardPlan={flow.discardPlan}
          confirming={flow.confirming}
          flowError={flow.error}
          awaitingAssistant={flow.awaitingAssistant}
          pendingQueue={flow.pendingQueue}
        />

        {flow.mode === 'plan' ? (
          <section className="graph">
            <div className="empty plan-mode-empty" style={{ marginTop: 120 }}>
              {task && plan
                ? 'Plan imported but no active session — confirm or start a session to execute.'
                : 'Plan Mode — chat with the planner to draft your first plan.'}
            </div>
          </section>
        ) : planQ.isLoading ? (
          <section className="graph">
            <div className="empty" style={{ marginTop: 120 }}>Loading plan…</div>
          </section>
        ) : planQ.isError || !plan ? (
          <section className="graph">
            <div className="empty" style={{ marginTop: 120 }}>
              No active plan — use Confirm Plan from chat.
            </div>
          </section>
        ) : showGraph ? (
          <Flow
            layout={layout}
            selected={selected}
            onSelect={selectNode}
            zoom={zoom}
            setZoom={setZoom}
            pan={pan}
            setPan={setPan}
            drawerOpen={drawerOpen}
            toggleDrawer={() => setDrawerOpen((o) => !o)}
            showGrid
            counts={counts}
            sessionId={flow.session?.session_id ?? null}
            sessionStatus={flow.session?.status ?? null}
          />
        ) : null}

        <Drawer
          open={drawerOpen}
          toggle={() => setDrawerOpen((o) => !o)}
          tab={tab}
          setTab={setTab}
          node={nodeQ.data ?? null}
          latestRun={runQ.data ?? null}
          onRerun={(id) => rerun.mutate(id)}
          onCancel={(runId) => cancelRun.mutate(runId)}
        />
      </div>
    </div>
  );
}
