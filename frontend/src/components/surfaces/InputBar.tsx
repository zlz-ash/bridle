import type { FormEvent } from 'react';
import { IconButton, Input } from '../ds';
import { ArrowUp, Messages } from '../ds/Icons';

export interface InputBarProps {
  draft: string;
  onDraft: (next: string) => void;
  onSend: (text: string) => void;
  onToggleHistory: () => void;
  historyExpanded: boolean;
  disabled?: boolean;
  disabledReason?: string;
  placeholder?: string;
  autoFocus?: boolean;
}

/** Permanent composer at the bottom-center. Always focuses input on click;
 *  the left icon and Ctrl+K toggle the conversation history only. */
export function InputBar({
  draft,
  onDraft,
  onSend,
  onToggleHistory,
  historyExpanded,
  disabled = false,
  disabledReason,
  placeholder,
  autoFocus,
}: InputBarProps) {
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text || disabled) return;
    onSend(text);
  };

  return (
    <div
      style={{
        position: 'absolute',
        left: '50%',
        bottom: 26,
        transform: 'translateX(-50%)',
        width: 'min(620px, calc(100% - 48px))',
        zIndex: 24,
      }}
    >
      <form onSubmit={submit}>
        <Input
          variant="pill"
          autoFocus={autoFocus}
          value={draft}
          onChange={(e) => onDraft(e.target.value)}
          placeholder={
            disabled && disabledReason
              ? disabledReason
              : (placeholder ?? 'Describe a task. Bridle will plan it.')
          }
          disabled={disabled}
          icon={
            <button
              type="button"
              onClick={onToggleHistory}
              aria-label={historyExpanded ? 'Collapse conversation' : 'Expand conversation'}
              title="Conversation history (Ctrl+K)"
              style={{
                display: 'grid',
                placeItems: 'center',
                width: 28,
                height: 28,
                marginLeft: -4,
                border: 'none',
                borderRadius: 'var(--radius-xs)',
                cursor: 'pointer',
                background: historyExpanded ? 'rgba(26,22,18,0.10)' : 'transparent',
                color: historyExpanded ? 'var(--text-ink-1)' : 'var(--text-ink-2)',
                transition: 'var(--transition-hover)',
              }}
            >
              <Messages size={17} />
            </button>
          }
          trailing={
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <kbd
                style={{
                  fontFamily: 'var(--font-mono)',
                  fontSize: 11,
                  color: 'var(--text-ink-3)',
                  border: '1px solid rgba(26,22,18,0.18)',
                  borderRadius: 6,
                  padding: '2px 6px',
                  lineHeight: 1,
                }}
              >
                Ctrl+K
              </kbd>
              <IconButton label="Send" type="submit" disabled={disabled || !draft.trim()}>
                <ArrowUp />
              </IconButton>
            </span>
          }
        />
      </form>
    </div>
  );
}
