import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { projectMapApi, projectSessionsApi, projectsApi } from '../api/endpoints';
import type { ChatMessage, PlanMapNode, ProjectRead, ProjectSession } from '../api/types';

/** Own project/session selection; no input and output is the single persisted conversation runtime. */
export function useProjectRuntime() {
  const queryClient = useQueryClient();
  const [activeProject, setActiveProject] = useState<ProjectRead | null>(null);
  const [activeSession, setActiveSession] = useState<ProjectSession | null>(null);

  const projectsQuery = useQuery({
    queryKey: ['projects'],
    queryFn: projectsApi.list,
    retry: false,
  });
  const sessionsQuery = useQuery({
    queryKey: ['project-sessions', activeProject?.id],
    queryFn: () => projectSessionsApi.list(activeProject!.id),
    enabled: activeProject !== null,
    retry: false,
  });
  const messagesQuery = useQuery({
    queryKey: ['project-messages', activeSession?.id],
    queryFn: () => projectSessionsApi.messages(activeSession!.id),
    enabled: activeSession !== null,
    retry: false,
    refetchInterval: 3_000,
  });

  const openMutation = useMutation({
    mutationFn: projectsApi.open,
    onSuccess: (project) => {
      setActiveProject(project);
      setActiveSession(null);
      void queryClient.invalidateQueries({ queryKey: ['projects'] });
    },
  });
  const createSessionMutation = useMutation({
    mutationFn: () => projectSessionsApi.create(activeProject!.id),
    onSuccess: (session) => {
      setActiveSession(session);
      void queryClient.invalidateQueries({ queryKey: ['project-sessions', session.project_id] });
    },
  });
  const roleMutation = useMutation({
    mutationFn: ({ role, confirmed }: { role: ProjectSession['role']; confirmed: boolean }) =>
      projectSessionsApi.changeRole(activeSession!.id, role, confirmed),
    onSuccess: setActiveSession,
  });
  const sendMutation = useMutation({
    mutationFn: ({ content, nodeId }: { content: string; nodeId?: string }) =>
      projectSessionsApi.sendMessage(activeSession!.id, content, nodeId),
    onMutate: async ({ content }) => {
      if (!activeSession) return undefined;
      const queryKey = ['project-messages', activeSession.id] as const;
      await queryClient.cancelQueries({ queryKey });
      const previous = queryClient.getQueryData<ChatMessage[]>(queryKey);
      const optimistic: ChatMessage = {
        id: `optimistic-${Date.now()}`,
        session_id: activeSession.id,
        role: 'user',
        content,
        tool_calls: null,
        tool_result: null,
        created_at: new Date().toISOString(),
      };
      queryClient.setQueryData<ChatMessage[]>(queryKey, (current) => [...(current ?? []), optimistic]);
      return { previous, queryKey };
    },
    onError: (_error, _content, context) => {
      if (context) queryClient.setQueryData(context.queryKey, context.previous);
    },
    onSettled: () => {
      if (activeSession) {
        void queryClient.invalidateQueries({ queryKey: ['project-messages', activeSession.id] });
      }
    },
  });

  /** Select registered history; project ID input exits with no implicit session selection. */
  const selectProject = (projectId: string) => {
    const project = projectsQuery.data?.projects.find((item) => item.id === projectId) ?? null;
    setActiveProject(project);
    setActiveSession(null);
  };
  /** Select one history item; session ID input exits as the active shared conversation. */
  const selectSession = (sessionId: string) => {
    const session = sessionsQuery.data?.find((item) => item.id === sessionId) ?? null;
    setActiveSession(session);
  };

  const chatDisabledReason = activeProject === null
    ? 'Open a project to chat'
    : !activeProject.can_chat
      ? activeProject.readiness_reason ?? activeProject.scan_status
      : activeSession === null
        ? 'Start a new conversation to chat'
        : !activeSession.available
          ? activeSession.readonly_reason ?? 'project_path_unavailable'
          : null;

  return {
    projectsQuery,
    sessionsQuery,
    messagesQuery,
    activeProject,
    activeSession,
    projects: projectsQuery.data?.projects ?? [],
    sessions: sessionsQuery.data ?? [],
    messages: messagesQuery.data ?? [],
    chatDisabled: chatDisabledReason !== null,
    chatDisabledReason,
    selectProject,
    selectSession,
    openProject: (path: string) => openMutation.mutateAsync(path),
    createSession: () => createSessionMutation.mutateAsync(),
    changeRole: (role: ProjectSession['role'], confirmed: boolean) =>
      roleMutation.mutateAsync({ role, confirmed }),
    sendMessage: (content: string, nodeId?: string) => sendMutation.mutateAsync({ content, nodeId }),
    openMutation,
    createSessionMutation,
    roleMutation,
    sendMutation,
  };
}

/** Load roots and requested branches; project ID input exits as one incrementally merged node list. */
export function useProgressivePlanMap(projectId: string | null) {
  const [nodesById, setNodesById] = useState<Map<string, PlanMapNode>>(() => new Map());
  const [changeSeq, setChangeSeq] = useState(0);
  const overviewQuery = useQuery({
    queryKey: ['project-map-overview', projectId],
    queryFn: () => projectMapApi.overview(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  useEffect(() => {
    setNodesById(new Map());
    setChangeSeq(0);
  }, [projectId]);
  useEffect(() => {
    if (!overviewQuery.data) return;
    setNodesById((current) => {
      const next = new Map(current);
      for (const node of overviewQuery.data.roots) next.set(node.id, node);
      return next;
    });
    setChangeSeq(overviewQuery.data.change_seq);
  }, [overviewQuery.data]);

  const changesQuery = useQuery({
    queryKey: ['project-map-changes', projectId, changeSeq],
    queryFn: () => projectMapApi.changes(projectId!, changeSeq),
    enabled: projectId !== null && overviewQuery.isSuccess,
    retry: false,
    refetchInterval: 2_000,
  });
  useEffect(() => {
    const changes = changesQuery.data;
    if (!changes || changes.items.length === 0 || !projectId) return;
    const changedNodeIds = [...new Set(
      changes.items
        .filter((item) => item.entity_type === 'plan_node' && item.operation !== 'remove')
        .map((item) => item.entity_id),
    )];
    void Promise.all(changedNodeIds.map((nodeId) => projectMapApi.node(projectId, nodeId))).then((nodes) => {
      setNodesById((current) => {
        const next = new Map(current);
        for (const item of changes.items) {
          if (item.entity_type === 'plan_node' && item.operation === 'remove') next.delete(item.entity_id);
        }
        for (const node of nodes) next.set(node.id, node);
        return next;
      });
      setChangeSeq(changes.last_seq);
    });
  }, [changesQuery.data, projectId]);

  /** Fetch one hierarchy level; parent input exits after merging only its direct children. */
  const expand = async (parentId: string) => {
    const page = await projectMapApi.children(projectId!, parentId, undefined, 100);
    setNodesById((current) => {
      const next = new Map(current);
      for (const node of page.items) next.set(node.id, node);
      return next;
    });
  };

  const nodes = useMemo(() => {
    /** Compute visible hierarchy depth; node input exits with a cycle-safe integer for parent-first order. */
    const depthOf = (node: PlanMapNode) => {
      let depth = 0;
      let parentId = node.parent_id;
      const seen = new Set<string>();
      while (parentId && !seen.has(parentId)) {
        seen.add(parentId);
        const parent = nodesById.get(parentId);
        if (!parent) break;
        depth += 1;
        parentId = parent.parent_id;
      }
      return depth;
    };
    return [...nodesById.values()].sort(
      (left, right) => depthOf(left) - depthOf(right) || left.order - right.order || left.id.localeCompare(right.id),
    );
  }, [nodesById]);
  return { nodes, expand, overviewQuery, changesQuery, changeSeq };
}
