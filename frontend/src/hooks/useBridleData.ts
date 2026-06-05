import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  tasksApi, planApi, nodesApi, codingSessionsApi, nodeAgentRunsApi,
} from '../api/endpoints';
import type { Task, ChatMessage } from '../api/types';
import { useEffect, useMemo, useState } from 'react';
import { deriveChatQueueState } from './deriveChatQueueState';

/** First task in the workspace — backend is single-task-driven in MVP. */
export function useFirstTask() {
  const q = useQuery({
    queryKey: ['tasks'],
    queryFn: () => tasksApi.list(),
    refetchInterval: 30_000,
  });
  const task: Task | null = q.data && q.data.length > 0 ? q.data[0] : null;
  return { task, ...q };
}

export function useCurrentPlan() {
  return useQuery({
    queryKey: ['plan', 'current'],
    queryFn: () => planApi.current(),
    refetchInterval: 10_000,
    retry: false,
  });
}

export function useNode(nodeId: string | null) {
  return useQuery({
    queryKey: ['node', nodeId],
    queryFn: () => nodesApi.get(nodeId!),
    enabled: !!nodeId,
    refetchInterval: 5_000,
  });
}

export function useLatestRun(nodeId: string | null) {
  return useQuery({
    queryKey: ['node-runs', nodeId],
    queryFn: async () => {
      const runs = await nodesApi.runs(nodeId!);
      return runs.length > 0 ? runs[0] : null;
    },
    enabled: !!nodeId,
    refetchInterval: 3_000,
  });
}

/**
 * The backend has no list-coding-sessions endpoint yet. As a pragmatic MVP we
 * persist the most recently-seen session id locally and re-fetch it; when the
 * backend adds GET /agent/coding-sessions (see plan.md), this hook becomes
 * data-driven instead.
 */
const _readStoredSessionId = (): string | null => {
  if (typeof localStorage === 'undefined') return null;
  const raw = localStorage.getItem('bridle.activeSessionId');
  // guard against legacy strings like "null" / "undefined" that crept in
  if (!raw || raw === 'null' || raw === 'undefined') return null;
  return raw;
};

export function useActiveSession(planId: string | null) {
  const [sessionId, setSessionId] = useState<string | null>(_readStoredSessionId);

  useEffect(() => {
    if (sessionId) localStorage.setItem('bridle.activeSessionId', sessionId);
  }, [sessionId]);

  // try fetching once we have one
  const q = useQuery({
    queryKey: ['coding-session', sessionId],
    queryFn: () => codingSessionsApi.get(sessionId!),
    enabled: !!sessionId,
    refetchInterval: 15_000,
    retry: false,
  });

  // if the stored id is for a different plan, drop it
  useEffect(() => {
    if (q.data && planId && q.data.plan_id !== planId) {
      setSessionId(null);
      localStorage.removeItem('bridle.activeSessionId');
    }
  }, [q.data, planId]);

  return { session: q.data ?? null, sessionId, setSessionId, ...q };
}

export function useChatMessages(sessionId: string | null) {
  const qc = useQueryClient();
  const validSessionId = sessionId && sessionId !== 'null' && sessionId !== 'undefined' ? sessionId : null;
  const q = useQuery({
    queryKey: ['chat', validSessionId],
    queryFn: () => codingSessionsApi.messages(validSessionId!),
    enabled: !!validSessionId,
    refetchInterval: 3_000,
  });

  const send = useMutation({
    mutationFn: (text: string) => codingSessionsApi.sendMessage(validSessionId!, text),
    onMutate: async (text) => {
      if (!validSessionId) return;
      await qc.cancelQueries({ queryKey: ['chat', validSessionId] });
      const prev = qc.getQueryData<ChatMessage[]>(['chat', validSessionId]);
      const optimistic: ChatMessage = {
        id: `optimistic-${Date.now()}`,
        session_id: validSessionId,
        role: 'user',
        content: text,
        tool_calls: null,
        tool_result: null,
        created_at: new Date().toISOString(),
      };
      qc.setQueryData<ChatMessage[]>(['chat', validSessionId], (old) => [...(old || []), optimistic]);
      return { prev };
    },
    onError: (_e, _t, ctx) => {
      if (ctx?.prev && validSessionId) qc.setQueryData(['chat', validSessionId], ctx.prev);
    },
    onSettled: () => {
      if (validSessionId) qc.invalidateQueries({ queryKey: ['chat', validSessionId] });
    },
  });

  const messages = q.data ?? [];
  const { awaitingAssistant, pendingQueue } = useMemo(
    () => deriveChatQueueState(messages),
    [messages],
  );

  return { messages, send, awaitingAssistant, pendingQueue, ...q };
}

export function useNodeMutations() {
  const qc = useQueryClient();
  const rerun = useMutation({
    mutationFn: (nodeId: string) => nodesApi.rerun(nodeId),
    onSuccess: (_d, nodeId) => {
      qc.invalidateQueries({ queryKey: ['node', nodeId] });
      qc.invalidateQueries({ queryKey: ['node-runs', nodeId] });
      qc.invalidateQueries({ queryKey: ['plan', 'current'] });
    },
  });
  const cancelRun = useMutation({
    mutationFn: (runId: string) => nodeAgentRunsApi.cancel(runId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['plan', 'current'] });
    },
  });
  return { rerun, cancelRun };
}

export function useCounts(statuses: string[]) {
  return useMemo(() => {
    const by = (s: string) => statuses.filter((x) => x === s).length;
    return {
      total: statuses.length,
      completed: by('completed'),
      running: by('running'),
      blocked: by('blocked'),
    };
  }, [statuses.join('|')]);
}
