import { useEffect, useRef, useState, Fragment } from 'react';
import { SendIcon } from './Icons';
import { WorkspaceSwitcher, type WorkspaceEntry } from './WorkspaceSwitcher';
import type { PlanImportPayload } from '../api/types';

export type ChatDisplayMessage = {
  id?: string;
  role: string;
  content: string;
  createdAt?: string;
};

interface Props {
  mode: 'plan' | 'execute';
  messages: ChatDisplayMessage[];
  onMention: (planNodeId: string) => void;
  onSend: (text: string) => void;
  width: number;
  setWidth: (w: number) => void;
  workspaces: WorkspaceEntry[];
  activeWs: string;
  onSwitch: (id: string) => void;
  onCreate: (ws: { name: string; path: string }) => void;
  canSend?: boolean;
  disabledReason?: string;
  proposedPlan?: PlanImportPayload | null;
  parseError?: string | null;
  onConfirmPlan?: () => void;
  onDiscardPlan?: () => void;
  confirming?: boolean;
  flowError?: string | null;
  awaitingAssistant?: boolean;
  pendingQueue?: { content: string }[];
}

function renderText(text: string, onMention: (id: string) => void) {
  const parts: React.ReactNode[] = [];
  const re = /#([a-zA-Z0-9_.-]+)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) parts.push(text.slice(last, m.index));
    const id = m[1];
    parts.push(
      <span key={`m${k++}`} className="mention" onClick={() => onMention(id)}>
        #{id}
      </span>,
    );
    last = m.index + m[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}

export function Chat({
  mode, messages, onMention, onSend, width, setWidth,
  workspaces, activeWs, onSwitch, onCreate,
  canSend = true, disabledReason,
  proposedPlan, parseError, onConfirmPlan, onDiscardPlan, confirming, flowError,
  awaitingAssistant = false,
  pendingQueue = [],
}: Props) {
  const [draft, setDraft] = useState('');
  const startRef = useRef<{ x: number; w: number } | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 200) + 'px';
  }, [draft]);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const s = startRef.current;
      if (!s) return;
      setWidth(Math.min(460, Math.max(248, s.w + (e.clientX - s.x))));
    };
    const onUp = () => {
      if (startRef.current) {
        startRef.current = null;
        document.body.style.cursor = '';
      }
    };
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [setWidth]);

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault();
    startRef.current = { x: e.clientX, w: width };
    document.body.style.cursor = 'col-resize';
  };

  const submit = () => {
    const text = draft.trim();
    if (!text || !canSend) return;
    onSend(text);
    setDraft('');
  };

  const banner =
    mode === 'plan'
      ? '📝 Plan Mode — drafting your plan with AI. Messages here are NOT saved until you confirm.'
      : '💬 Execute Mode — conversation is persisted and tied to plan.';

  const inputEmpty = draft.trim().length === 0;
  const showWaiting = awaitingAssistant && inputEmpty;

  const placeholder =
    mode === 'plan'
      ? 'Tell the AI what you want to build…'
      : awaitingAssistant
        ? '主代理正在处理上一条消息，可继续输入下一条…'
        : (canSend ? 'Message the agent…' : (disabledReason || 'No active session'));

  return (
    <aside className="chat" style={{ width, flexBasis: width }}>
      <div className="chat-head">
        <WorkspaceSwitcher
          workspaces={workspaces}
          activeId={activeWs}
          onSwitch={onSwitch}
          onCreate={onCreate}
        />
      </div>
      <div className={`chat-banner ${mode === 'plan' ? 'plan' : 'execute'}`}>{banner}</div>
      {flowError && <div className="chat-error">{flowError}</div>}
      <div className="chat-log" ref={logRef}>
        {messages.length === 0 && (
          <div style={{ color: 'var(--faint)', fontStyle: 'italic', fontFamily: 'var(--serif)' }}>
            {mode === 'plan' ? 'Start by describing what you want to build.' : 'No messages yet.'}
          </div>
        )}
        {messages.map((msg, idx) => (
          <Fragment key={msg.id || `${msg.role}-${msg.content.slice(0, 24)}`}>
            {msg.role === 'system' ? (
              <div className="msg ai" style={{ color: 'var(--muted)', fontStyle: 'italic' }}>
                {renderText(msg.content, onMention)}
              </div>
            ) : (
              <div className={'msg ' + (msg.role === 'user' ? 'user' : 'ai')}>
                {renderText(msg.content, onMention)}
              </div>
            )}
            {awaitingAssistant
              && pendingQueue.length > 0
              && idx === messages.length - 1
              && msg.role === 'user' && (
              <div className="chat-waiting-hint">⏳ 等待主代理回复...</div>
            )}
          </Fragment>
        ))}
      </div>
      {mode === 'plan' && parseError && !proposedPlan && (
        <div className="plan-proposal-dock">
          <div className="plan-proposal-card">
            <div className="plan-proposal-title">Plan parse failed</div>
            <div className="plan-proposal-warning" style={{ marginTop: 6 }}>
              {parseError}
            </div>
            <div className="plan-proposal-meta" style={{ marginTop: 8 }}>
              AI 返回的 JSON 没法解析。继续聊一句"请重新输出符合 PlanImportSchema 的完整 JSON"通常能修好。
            </div>
          </div>
        </div>
      )}
      {proposedPlan && mode === 'plan' && (
        <div className="plan-proposal-dock">
          <div className="plan-proposal-card">
            <button
              type="button"
              className="plan-proposal-close"
              title="Discard plan"
              disabled={confirming}
              onClick={onDiscardPlan}
              aria-label="Discard plan"
            >
              ×
            </button>
            <div className="plan-proposal-title">Proposed plan</div>
            <div className="plan-proposal-goal">{proposedPlan.goal}</div>
            <div className="plan-proposal-meta">{proposedPlan.nodes.length} node(s)</div>
            {parseError && (
              <div className="plan-proposal-warning">Plan parse issue: {parseError}</div>
            )}
            <div className="plan-proposal-actions">
              <button type="button" className="confirm-plan-btn" disabled={confirming} onClick={onConfirmPlan}>
                {confirming ? 'Confirming…' : 'Confirm Plan'}
              </button>
              <button type="button" className="discard-plan-btn" disabled={confirming} onClick={onDiscardPlan}>
                Discard, keep talking
              </button>
            </div>
          </div>
        </div>
      )}
      {pendingQueue.length > 0 && (
        <div className="chat-queue-bar" title={pendingQueue.map((m) => m.content).join('\n')}>
          队列中：{pendingQueue.length} 条消息等待主代理回应
        </div>
      )}
      <div className="chat-input">
        <textarea
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (!showWaiting) submit();
            }
          }}
          placeholder={placeholder}
          disabled={!canSend}
          rows={1}
        />
        <button
          className={'send' + (showWaiting ? ' waiting' : '')}
          title={
            showWaiting
              ? '等待主代理回复'
              : (canSend ? 'Send' : (disabledReason || 'Unavailable'))
          }
          onClick={submit}
          disabled={!canSend || showWaiting || inputEmpty}
        >
          {showWaiting ? <span className="send-spinner" aria-hidden /> : <SendIcon />}
        </button>
      </div>
      <div className="chat-resizer" onMouseDown={startResize} title="Drag to resize" />
    </aside>
  );
}
