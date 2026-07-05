import { useEffect, useMemo, useState } from 'react';
import { Flow } from '../components/Flow';
import { Header, type HeaderSession, type HeaderStats } from '../components/surfaces/Header';
import { ConversationCard } from '../components/surfaces/ConversationCard';
import { InputBar } from '../components/surfaces/InputBar';
import { RightInspector, type InspectorTab } from '../components/surfaces/RightInspector';
import { WorkspaceSwitcher, type WorkspaceEntry } from '../components/WorkspaceSwitcher';
import { Button } from '../components/ds';
import { computeLayout } from './computeLayout';
import { computeSemanticLayout } from './computeSemanticLayout';
import { useProgressivePlanMap, useProjectRuntime } from '../hooks/useProjectRuntime';
import { useProjectMapLayers } from '../hooks/useProjectMapLayers';
import { projectMapApi } from '../api/endpoints';
import type { NodeRead, PlanMapNode } from '../api/types';

const INSPECTOR_W_KEY = 'bridle.inspector.w';

/** Read the saved drawer width; no input exits as a bounded pixel width. */
function loadInspectorWidth(): number {
  const raw = Number(localStorage.getItem(INSPECTOR_W_KEY));
  return Number.isFinite(raw) && raw >= 280 && raw <= 720 ? raw : 400;
}

/** Adapt a map node for the existing inspector; map input exits without a full-plan fetch. */
function toInspectorNode(node: PlanMapNode, projectId: string): NodeRead {
  const now = new Date(0).toISOString();
  const files = Array.isArray(node.files) ? node.files.map(String) : [];
  const tests = Array.isArray(node.tests) ? node.tests.map(String) : [];
  return {
    id: node.id,
    plan_id: projectId,
    plan_node_id: node.id,
    title: node.title,
    goal: node.goal,
    node_type: node.node_type,
    order: node.order,
    depends_on: node.depends_on,
    files,
    tests,
    test_details: tests.map((command) => ({ command, purpose: '' })),
    test_count: tests.length,
    metrics: {}, constraints: {}, review_checks: [], expected_outputs: {}, interfaces: {},
    read_set: [], write_set: [], readonly_context: [], conflict_contributions: [],
    status: node.status,
    created_at: now,
    updated_at: now,
  };
}

/** Render the project shell; runtime hook inputs exit as project-scoped map and chat UI. */
export function ProjectAppShell() {
  const runtime = useProjectRuntime();
  const planMap = useProgressivePlanMap(runtime.activeProject?.id ?? null);
  const mapLayers = useProjectMapLayers(runtime.activeProject?.id ?? null);
  const [mapView, setMapView] = useState<'plan' | 'semantic'>('plan');
  const [selected, setSelected] = useState<string | null>(null);
  const [nodeDetail, setNodeDetail] = useState<PlanMapNode | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [tab, setTab] = useState<InspectorTab>('Node Info');
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [inspectorW, setInspectorW] = useState(loadInspectorWidth);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [draft, setDraft] = useState('');

  useEffect(() => {
    localStorage.setItem(INSPECTOR_W_KEY, String(Math.round(inspectorW)));
  }, [inspectorW]);
  useEffect(() => {
    setSelected(null);
    setInspectorOpen(false);
    setHistoryOpen(true);
    setMapView('plan');
    setNodeDetail(null);
  }, [runtime.activeProject?.id, runtime.activeSession?.id]);

  useEffect(() => {
    if (!selected || !runtime.activeProject || mapView !== 'plan') {
      setNodeDetail(null);
      return;
    }
    void projectMapApi.node(runtime.activeProject.id, selected).then(setNodeDetail).catch(() => setNodeDetail(null));
  }, [selected, runtime.activeProject?.id, mapView]);
  useEffect(() => {
    /** Bind conversation history; keyboard input exits by toggling the session overlay. */
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey && (event.key === 'k' || event.key === 'K')) {
        event.preventDefault();
        setHistoryOpen((open) => !open);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  const workspaces: WorkspaceEntry[] = runtime.projects.map((project) => ({
    id: project.id,
    name: project.name,
    path: project.path,
  }));
  const blindSpotEntityIds = useMemo(() => {
    const ids = new Set<string>();
    for (const spot of mapLayers.blindSpots) {
      if (!spot.file_path) continue;
      for (const entity of mapLayers.entities) {
        if (entity.path === spot.file_path || entity.path.startsWith(`${spot.file_path}::`)) {
          ids.add(entity.id);
        }
      }
    }
    return ids;
  }, [mapLayers.blindSpots, mapLayers.entities]);
  const planLayout = useMemo(() => computeLayout(planMap.nodes), [planMap.nodes]);
  const semanticLayout = useMemo(
    () => computeSemanticLayout(
      mapLayers.entities,
      mapLayers.debtNodes,
      mapLayers.relations,
      mapLayers.activeAnnotations,
      blindSpotEntityIds,
      mapLayers.renderLimit,
    ),
    [
      mapLayers.entities,
      mapLayers.debtNodes,
      mapLayers.relations,
      mapLayers.activeAnnotations,
      blindSpotEntityIds,
      mapLayers.renderLimit,
    ],
  );
  const layout = mapView === 'plan'
    ? planLayout
    : { nodes: semanticLayout.nodes, edges: semanticLayout.edges, lane: semanticLayout.lane };
  const counts = useMemo(() => ({
    total: layout.nodes.length,
    completed: planLayout.nodes.filter((node) => node.status === 'completed').length,
    running: planLayout.nodes.filter((node) => node.status === 'running').length,
    blocked: planLayout.nodes.filter((node) => node.status === 'blocked' || node.status === 'drifted').length,
  }), [layout.nodes.length, planLayout.nodes]);
  const selectedMapNode = planMap.nodes.find((node) => node.id === selected) ?? nodeDetail;
  const selectedInspectorNode = selectedMapNode && runtime.activeProject && mapView === 'plan'
    ? toInspectorNode(selectedMapNode, runtime.activeProject.id)
    : null;

  const headerStats: HeaderStats = {
    total: counts.total,
    done: counts.completed,
    running: counts.running,
    blocked: counts.blocked,
  };
  const headerSession: HeaderSession = {
    workspace: runtime.activeProject?.name ?? 'No project',
    id: runtime.activeSession?.id.slice(0, 8) ?? null,
    status: counts.running > 0 ? 'running' : counts.blocked > 0 ? 'failed' : counts.completed > 0 ? 'completed' : 'idle',
  };

  /** Select a map node; node ID input exits with the existing inspector opened. */
  const selectNode = (id: string | null) => {
    setSelected(id);
    if (id) {
      setInspectorOpen(true);
      setTab('Node Info');
    }
  };
  /** Submit one persisted message; text input exits after the composer clears. */
  const handleSend = (text: string) => {
    setDraft('');
    const nodeId = runtime.activeSession?.role === 'executing' ? selected ?? undefined : undefined;
    void runtime.sendMessage(text, nodeId);
  };
  /** Change roles only from a user action; target input exits after explicit confirmation. */
  const requestRoleChange = (target: 'planning' | 'executing') => {
    const prompt = target === 'executing'
      ? 'Enter execution mode? The agent may modify project source files.'
      : 'Return to planning mode? Source editing will be disabled.';
    if (window.confirm(prompt)) void runtime.changeRole(target, true);
  };

  const actionBar = runtime.activeProject ? (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <Button
        size="sm"
        variant={mapView === 'plan' ? 'primary' : 'ghost'}
        onClick={() => setMapView('plan')}
      >
        Plan map
      </Button>
      <Button
        size="sm"
        variant={mapView === 'semantic' ? 'primary' : 'ghost'}
        onClick={() => setMapView('semantic')}
      >
        Semantic map
      </Button>
      <span style={{ color: 'var(--text-cream-3)', fontSize: 12 }}>
        {semanticLayout.totalEntityCount} 实体 · 已渲染 {semanticLayout.renderedCount}
        {semanticLayout.renderTruncated ? ` / ${semanticLayout.totalEntityCount}` : ''}
        {mapLayers.entitiesTruncated ? ' · 抓取上限' : ''}
      </span>
      {semanticLayout.renderTruncated ? (
        <Button
          size="sm"
          variant="ghost"
          onClick={() => mapLayers.setRenderLimit((limit) => limit + 200)}
        >
          显示更多节点
        </Button>
      ) : null}
      <Button size="sm" variant="ghost" onClick={() => void runtime.createSession()}>
        New conversation
      </Button>
      {runtime.sessions.map((session) => (
        <Button
          key={session.id}
          size="sm"
          variant={session.id === runtime.activeSession?.id ? 'primary' : 'ghost'}
          onClick={() => runtime.selectSession(session.id)}
        >
          {session.title}
        </Button>
      ))}
      {runtime.activeSession ? (
        <Button
          size="sm"
          variant="primary"
          disabled={runtime.roleMutation.isPending || !runtime.activeSession.available}
          onClick={() => requestRoleChange(runtime.activeSession!.role === 'planning' ? 'executing' : 'planning')}
        >
          {runtime.activeSession.role === 'planning' ? 'Enter execution' : 'Return to planning'}
        </Button>
      ) : null}
      {runtime.activeSession?.readonly_reason ? (
        <span style={{ color: 'var(--accent-copper)', fontSize: 12 }}>{runtime.activeSession.readonly_reason}</span>
      ) : null}
    </div>
  ) : null;

  const noProject = runtime.activeProject === null;
  const noSession = runtime.activeProject !== null && runtime.activeSession === null;
  const conversationVisible = historyOpen || noProject || noSession;
  const inputReason = noProject
    ? 'Open a project to chat'
    : noSession
      ? 'Start a new conversation to chat'
      : runtime.chatDisabledReason
        ?? runtime.activeSession?.readonly_reason
        ?? (runtime.activeSession?.role === 'executing' && !selected
          ? 'Select a plan node before executing'
          : undefined);

  return (
    <div className="bridle-shell">
      <Header
        session={headerSession}
        stats={headerStats}
        inspectorOpen={inspectorOpen}
        onToggleInspector={() => setInspectorOpen((open) => !open)}
        workspaceSlot={(
          <div className="bridle-workspace-slot">
            <WorkspaceSwitcher
              workspaces={workspaces}
              activeId={runtime.activeProject?.id ?? null}
              onSwitch={runtime.selectProject}
              onCreate={({ path }) => { void runtime.openProject(path); }}
            />
          </div>
        )}
        onZoomIn={() => setZoom((value) => Math.min(2, value + 0.1))}
        onZoomOut={() => setZoom((value) => Math.max(0.5, value - 0.1))}
        onZoomFit={() => { setZoom(1); setPan({ x: 0, y: 0 }); }}
      />

      <div className="bridle-body">
        <div className={'bridle-canvas-zone' + (conversationVisible ? ' flow--overlay' : '')}>
          <div className="bridle-dot-grid" />
          {noProject ? (
            <div className="bridle-canvas-empty">Open a project to begin</div>
          ) : planMap.overviewQuery.isLoading && mapView === 'plan' ? (
            <div className="bridle-canvas-empty">Loading project map…</div>
          ) : mapLayers.entitiesQuery.isLoading && mapView === 'semantic' ? (
            <div className="bridle-canvas-empty">Loading semantic map…</div>
          ) : layout.nodes.length > 0 ? (
            <Flow
              layout={layout}
              selected={selected}
              onSelect={selectNode}
              onExpand={mapView === 'plan' ? (id) => { void planMap.expand(id); } : undefined}
              zoom={zoom}
              setZoom={setZoom}
              pan={pan}
              setPan={setPan}
              drawerOpen={inspectorOpen}
              toggleDrawer={() => setInspectorOpen((open) => !open)}
              showGrid={false}
              counts={counts}
              sessionId={runtime.activeSession?.id ?? null}
              sessionStatus={runtime.activeSession?.status ?? null}
              mapView={mapView}
              layerCounts={{
                codeEntities: mapLayers.entities.length,
                blindSpots: mapLayers.blindSpots.length,
                debt: mapLayers.debtNodes.length,
              }}
            />
          ) : (
            <div className="bridle-canvas-empty">
              {mapView === 'plan' ? 'This project map is empty' : 'No code entities indexed yet'}
            </div>
          )}

          {conversationVisible ? (
            <ConversationCard
              messages={runtime.messages.map((message) => ({
                id: message.id,
                role: message.role,
                content: message.content,
                createdAt: message.created_at,
              }))}
              streaming={runtime.sendMutation.isPending}
              actionBar={actionBar}
              onCollapse={noProject || noSession ? undefined : () => setHistoryOpen(false)}
            />
          ) : null}

          <InputBar
            draft={draft}
            onDraft={setDraft}
            onSend={handleSend}
            onToggleHistory={() => setHistoryOpen((open) => !open)}
            historyExpanded={conversationVisible}
            disabled={runtime.chatDisabled || runtime.sendMutation.isPending || Boolean(inputReason)}
            disabledReason={inputReason}
            placeholder={runtime.activeSession?.role === 'executing'
              ? 'Ask Bridle to execute the next plan node.'
              : 'Refine the project plan.'}
            autoFocus={!runtime.chatDisabled}
          />
        </div>

        <RightInspector
          open={inspectorOpen}
          tab={tab}
          setTab={setTab}
          width={inspectorW}
          setWidth={setInspectorW}
          node={selectedInspectorNode}
          workspaceLabel={runtime.activeProject?.name ?? null}
          sessionId={runtime.activeSession?.id.slice(0, 8) ?? null}
          blindSpots={mapLayers.blindSpots}
          pendingArbitrationCount={mapLayers.pendingArbitration.length}
        />
      </div>
    </div>
  );
}
