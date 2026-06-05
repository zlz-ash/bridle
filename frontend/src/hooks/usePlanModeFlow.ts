import { useCallback, useMemo, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { planModeApi, workspaceApi } from '../api/planMode';
import { parseApiError } from '../api/client';
import { tasksApi, planApi, codingSessionsApi } from '../api/endpoints';
import type { PlanImportPayload } from '../api/types';
import { useDraftChat, type DraftChatMessage } from './useDraftChat';
import { useActiveSession, useChatMessages, useCurrentPlan } from './useBridleData';

export function usePlanModeFlow(workspaceId: string | null) {
  const qc = useQueryClient();
  const planQ = useCurrentPlan();
  const plan = planQ.data ?? null;
  const { session, setSessionId } = useActiveSession(plan?.id ?? null);
  const draft = useDraftChat(workspaceId);
  const {
    messages: execMessages,
    send: execSend,
    awaitingAssistant,
    pendingQueue,
  } = useChatMessages(session?.session_id ?? null);

  const [proposedPlan, setProposedPlan] = useState<PlanImportPayload | null>(null);
  const [parseError, setParseError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mode: 'plan' | 'execute' = plan && session ? 'execute' : 'plan';

  const messages = useMemo(() => {
    if (mode === 'execute') {
      return execMessages.map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        createdAt: m.created_at,
      }));
    }
    return draft.messages.map((m, i) => ({
      id: `draft-${i}-${m.createdAt}`,
      role: m.role,
      content: m.content,
      createdAt: m.createdAt,
    }));
  }, [mode, execMessages, draft.messages]);

  const send = useCallback(
    async (text: string) => {
      setError(null);
      if (mode === 'execute') {
        execSend.mutate(text);
        return;
      }
      setProposedPlan(null);
      setParseError(null);
      const userMsg: DraftChatMessage = {
        role: 'user',
        content: text,
        createdAt: new Date().toISOString(),
      };
      draft.append(userMsg);
      setSending(true);
      try {
        const overview = await workspaceApi.overview();
        const history = [...draft.messages, userMsg].map((m) => ({
          role: m.role,
          content: m.content,
        }));
        const resp = await planModeApi.converse(history, overview);
        draft.append({
          role: 'assistant',
          content: resp.reply,
          createdAt: new Date().toISOString(),
        });
        setProposedPlan(resp.proposed_plan ?? null);
        setParseError(resp.parse_error ?? null);
      } catch (err) {
        setError(parseApiError(err).message);
      } finally {
        setSending(false);
      }
    },
    [mode, execSend, draft],
  );

  const discardPlan = useCallback(() => {
    setProposedPlan(null);
    setParseError(null);
  }, []);

  const confirmPlan = useCallback(async () => {
    if (!proposedPlan || confirming) return;
    setConfirming(true);
    setError(null);
    try {
      const task = await tasksApi.create({
        title: proposedPlan.goal.slice(0, 60),
        goal: proposedPlan.goal,
      });
      const imported = await tasksApi.importPlan(task.id, proposedPlan);
      const sess = await codingSessionsApi.create({
        plan_id: imported.plan_id,
        auto_continue_budget: 5,
      });
      // Backend returns status='creating' immediately and spins the main-agent container
      // in the background. Poll the session until it flips to 'active' or 'failed'.
      const sessionId = sess.session_id;
      const pollDeadlineMs = Date.now() + 90_000; // 90s ceiling for docker startup
      const pollIntervalMs = 1500;
      let finalStatus = sess.status;
      while (finalStatus === 'creating' && Date.now() < pollDeadlineMs) {
        await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
        const polled = await codingSessionsApi.get(sessionId);
        finalStatus = polled.status;
      }
      if (finalStatus === 'failed') {
        throw new Error('Session 创建失败：main-agent 容器启动出错，请查看 docker logs。');
      }
      if (finalStatus === 'creating') {
        throw new Error('Session 创建超时（90s）：main-agent 容器仍在启动，请稍后刷新查看状态。');
      }
      // Refresh plan & tasks BEFORE flipping sessionId, so mode = (plan && session)
      // can transition to 'execute' on the very next render instead of waiting for
      // the 10s refetchInterval tick on useCurrentPlan to pick up the new plan.
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['plan', 'current'], refetchType: 'active' }),
        qc.invalidateQueries({ queryKey: ['tasks'], refetchType: 'active' }),
      ]);
      setSessionId(sessionId);
      draft.clear();
      setProposedPlan(null);
      setParseError(null);
      await qc.invalidateQueries({ queryKey: ['coding-session'] });
    } catch (err) {
      const apiErr = parseApiError(err);
      if (apiErr.code === 'plan_not_executable' && apiErr.details?.last_issues) {
        const issues = apiErr.details.last_issues as Array<{
          node_id?: string;
          issues?: string[];
        }>;
        const lines = issues.flatMap((item) => {
          const id = item.node_id ?? '?';
          const list = item.issues ?? [];
          return list.map((issue) => `• ${id}: ${issue}`);
        });
        setError(
          [apiErr.message, ...lines].filter(Boolean).join('\n'),
        );
      } else {
        setError(apiErr.message);
      }
    } finally {
      setConfirming(false);
    }
  }, [proposedPlan, confirming, draft, setSessionId, qc]);

  return {
    mode,
    messages,
    send,
    awaitingAssistant: mode === 'execute' ? awaitingAssistant : false,
    pendingQueue: mode === 'execute' ? pendingQueue : [],
    confirmPlan,
    discardPlan,
    proposedPlan,
    parseError,
    confirming,
    sending,
    error,
    session,
    canSend: mode === 'plan' ? !sending && !confirming : !!session?.session_id,
  };
}
