import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { ConversationBubble } from '../ds';
import { ChevronUp } from '../ds/Icons';

export interface ConversationMessage {
  id?: string;
  /** 'user' | 'assistant' | 'system' | 'tool' from the API; mapped to bubble side. */
  role: string;
  content: string;
  createdAt?: string;
}

export interface ConversationCardProps {
  messages: ConversationMessage[];
  /** Show a blinking caret on the last agent message. */
  streaming?: boolean;
  /** Rendered above the input gap — typically the plan-confirm / pending banner. */
  actionBar?: ReactNode;
  /** Render arbitrary content inside a message bubble (e.g. with #mentions). */
  renderContent?: (text: string, role: string) => ReactNode;
  /** Omit to hide the collapse affordance (e.g. when the card is the only surface for input). */
  onCollapse?: () => void;
}

function bubbleFrom(role: string): 'user' | 'agent' {
  return role === 'user' ? 'user' : 'agent';
}

function formatTime(iso: string | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Expanded conversation — centered cream-glass card, top-down scrollable chat. */
export function ConversationCard({
  messages,
  streaming = false,
  actionBar,
  renderContent,
  onCollapse,
}: ConversationCardProps) {
  const bodyRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, streaming]);

  return (
    <div
      style={{
        position: 'absolute',
        left: 0,
        right: 0,
        marginLeft: 'auto',
        marginRight: 'auto',
        top: 72,
        bottom: 98,
        width: 'min(720px, calc(100% - 48px))',
        zIndex: 20,
        background: 'rgba(248,230,195,0.85)',
        backdropFilter: 'blur(20px) saturate(150%)',
        WebkitBackdropFilter: 'blur(20px) saturate(150%)',
        borderRadius: 'var(--radius-card)',
        border: '1px solid rgba(245,232,208,0.5)',
        boxShadow: 'var(--shadow-card)',
        display: 'flex',
        flexDirection: 'column',
        overflow: 'hidden',
        animation: 'bridle-overlay-in var(--dur-overlay) var(--ease-overlay)',
      }}
    >
      {onCollapse ? (
        <button
          type="button"
          onClick={onCollapse}
          aria-label="Collapse conversation"
          title="Collapse (Ctrl+K)"
          className="brd-card-collapse"
          style={{
            position: 'absolute',
            top: 10,
            right: 10,
            zIndex: 2,
            display: 'grid',
            placeItems: 'center',
            width: 28,
            height: 28,
            border: 'none',
            background: 'rgba(245,232,208,0.6)',
            color: 'var(--text-ink-2)',
            cursor: 'pointer',
            borderRadius: 'var(--radius-xs)',
            transition: 'var(--transition-hover)',
          }}
        >
          <ChevronUp size={16} />
        </button>
      ) : null}

      <div
        ref={bodyRef}
        className="brd-chat-scroll"
        style={{
          flex: 1,
          overflowY: 'auto',
          padding: '24px 18px 18px',
          display: 'flex',
          flexDirection: 'column',
          gap: 14,
          WebkitMaskImage: 'linear-gradient(to bottom, transparent, #000 24px)',
          maskImage: 'linear-gradient(to bottom, transparent, #000 24px)',
        }}
      >
        {messages.length === 0 ? (
          <div
            style={{
              margin: 'auto 0',
              textAlign: 'center',
              color: 'var(--text-ink-2)',
              fontSize: 13,
            }}
          >
            Start by describing a task. Bridle will plan it.
          </div>
        ) : (
          messages.map((m, i) => {
            const from = bubbleFrom(m.role);
            const time = formatTime(m.createdAt);
            const isLastAgent = streaming && i === messages.length - 1 && from === 'agent';
            return (
              <div
                key={m.id ?? `${i}-${m.createdAt ?? ''}`}
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: from === 'user' ? 'flex-end' : 'flex-start',
                  gap: 4,
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    gap: 7,
                    fontSize: 11,
                    color: 'var(--text-ink-2)',
                    padding: '0 2px',
                  }}
                >
                  <span style={{ fontWeight: 600 }}>{from === 'user' ? 'You' : 'Bridle'}</span>
                  {time ? (
                    <span style={{ fontFamily: 'var(--font-mono)', opacity: 0.7 }}>{time}</span>
                  ) : null}
                </div>
                <ConversationBubble from={from} streaming={isLastAgent}>
                  {renderContent ? renderContent(m.content, m.role) : m.content}
                </ConversationBubble>
              </div>
            );
          })
        )}
      </div>

      {actionBar ? (
        <div
          style={{
            flex: 'none',
            padding: '12px 16px',
            borderTop: '1px solid rgba(26,22,18,0.10)',
            background: 'rgba(245,232,208,0.4)',
          }}
        >
          {actionBar}
        </div>
      ) : null}
    </div>
  );
}
