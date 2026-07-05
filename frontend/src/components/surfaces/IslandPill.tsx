import { useState } from 'react';
import { ChevronDown } from '../ds/Icons';

export type IslandStatusTone = 'idle' | 'running' | 'completed' | 'failed';

export interface IslandStatus {
  tone: IslandStatusTone;
  /** Plain-text status line; rendered without HTML. */
  text: string;
}

export interface IslandPillProps {
  status: IslandStatus;
  onExpand: () => void;
}

const DOT: Record<IslandStatusTone, string> = {
  running: 'var(--accent-amber)',
  completed: 'var(--accent-sage)',
  failed: 'var(--accent-copper)',
  idle: 'var(--text-cream-3)',
};

/** Dynamic-Island-style collapsed conversation strip. */
export function IslandPill({ status, onExpand }: IslandPillProps) {
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onExpand}
      aria-label="Expand conversation"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: 'absolute',
        top: 24,
        left: '50%',
        zIndex: 22,
        transform: `translateX(-50%) scale(${hover ? 1.02 : 1})`,
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        maxWidth: 480,
        height: 36,
        padding: '0 16px',
        borderRadius: 18,
        background: 'rgba(245,232,208,0.55)',
        backdropFilter: 'blur(24px)',
        WebkitBackdropFilter: 'blur(24px)',
        border: '1px solid rgba(255,200,87,0.20)',
        boxShadow: '0 8px 24px rgba(0,0,0,0.35)',
        cursor: 'pointer',
        font: 'inherit',
        filter: hover ? 'brightness(1.05)' : 'none',
        transition: 'transform 160ms var(--ease-out), filter 160ms var(--ease-out)',
        animation: 'bridle-island-in 200ms var(--ease-overlay)',
      }}
    >
      <span
        style={{
          width: 7,
          height: 7,
          borderRadius: '50%',
          flex: 'none',
          background: DOT[status.tone],
          animation:
            status.tone === 'running' ? 'bridle-blink 1.4s var(--ease-in-out) infinite' : 'none',
        }}
      />
      <span
        style={{
          flex: 1,
          minWidth: 0,
          fontSize: 13,
          color: 'var(--text-ink-1)',
          whiteSpace: 'nowrap',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
      >
        {status.text}
      </span>
      <span
        style={{
          display: 'flex',
          color: 'var(--text-ink-2)',
          flex: 'none',
          marginRight: -4,
        }}
      >
        <ChevronDown size={15} />
      </span>
    </button>
  );
}
