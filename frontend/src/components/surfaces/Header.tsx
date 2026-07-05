import type { CSSProperties } from 'react';
import { IconButton } from '../ds';
import {
  ChevronDown,
  ChevronRight,
  PanelRight,
  Retry,
  ZoomIn,
  ZoomOut,
  Maximize,
} from '../ds/Icons';
import logomark from '../../assets/logomark.svg';

export interface HeaderSession {
  /** Active workspace name shown in the toolbar dropdown. */
  workspace: string | null;
  /** Short session id (display only). */
  id: string | null;
  /** Active session status — drives the leading dot color. */
  status: 'idle' | 'running' | 'completed' | 'failed' | null;
}

export interface HeaderStats {
  total: number;
  done: number;
  running: number;
  blocked: number;
}

export interface HeaderProps {
  session: HeaderSession;
  stats: HeaderStats;
  inspectorOpen: boolean;
  onToggleInspector: () => void;
  onZoomIn?: () => void;
  onZoomOut?: () => void;
  onZoomFit?: () => void;
  /** Optional workspace switcher slot (rendered between dot and stats). */
  workspaceSlot?: React.ReactNode;
  /** Destructive reset of Bridle runtime state for the active workspace. */
  onReset?: () => void;
  resetting?: boolean;
}

function Stat({
  value,
  label,
  tone,
}: {
  value: number | string;
  label: string;
  tone?: string;
}) {
  return (
    <span style={{ color: 'var(--text-cream-3)' }}>
      <span style={{ color: tone || 'var(--text-cream-1)', fontWeight: 600 }}>{value}</span>{' '}
      {label}
    </span>
  );
}

const sepStyle: CSSProperties = { width: 1, height: 14, background: 'var(--hairline)' };

/** Two-tier header: product bar (brand + eyebrow + theme) + workspace toolbar
 *  (workspace + session stats + zoom + inspector toggle). */
export function Header({
  session,
  stats,
  inspectorOpen,
  onToggleInspector,
  onZoomIn,
  onZoomOut,
  onZoomFit,
  workspaceSlot,
  onReset,
  resetting = false,
}: HeaderProps) {
  const dotColor =
    session.status === 'running'
      ? 'var(--accent-amber)'
      : session.status === 'failed'
        ? 'var(--accent-copper)'
        : session.status === 'completed'
          ? 'var(--accent-sage)'
          : 'var(--text-cream-3)';

  return (
    <div className="brd-header" style={{ flex: 'none', position: 'relative', zIndex: 30 }}>
      {/* Tier 1 — product (compact: half the original height) */}
      <header
        style={{
          height: 24,
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '0 14px',
          background: 'var(--panel)',
          borderBottom: '1px solid var(--hairline)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
          <img src={logomark} width={14} height={14} alt="" />
          <span
            style={{
              color: 'var(--text-cream-1)',
              fontWeight: 600,
              fontSize: 12,
              letterSpacing: '-0.02em',
              lineHeight: 1,
            }}
          >
            Bridle
          </span>
        </div>
        <div style={{ width: 1, height: 11, background: 'var(--hairline)' }} />
        <span
          style={{
            fontSize: 9,
            fontWeight: 600,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: 'var(--text-cream-3)',
            lineHeight: 1,
          }}
        >
          Agent Workspace
        </span>
        <div style={{ flex: 1 }} />
      </header>

      {/* Tier 2 — workspace toolbar */}
      <div
        style={{
          height: 38,
          display: 'flex',
          alignItems: 'center',
          gap: 12,
          padding: '0 12px 0 16px',
          background: 'rgba(0,0,0,0.18)',
          borderBottom: '1px solid var(--hairline)',
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            flex: 'none',
            background: dotColor,
          }}
        />
        {workspaceSlot ?? (
          <span
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              color: 'var(--text-cream-1)',
              fontSize: 13,
            }}
          >
            {session.workspace ?? 'workspace'}
            <ChevronDown size={14} />
          </span>
        )}
        {onReset ? (
          <IconButton
            label={resetting ? 'Resetting…' : 'Reset Bridle workspace'}
            size="sm"
            onClick={onReset}
            disabled={resetting}
            style={{ color: 'var(--accent-copper)' }}
          >
            <Retry size={14} />
          </IconButton>
        ) : null}
        <div style={sepStyle} />
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 8,
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            fontFeatureSettings: 'var(--mono-features)',
            minWidth: 0,
            overflow: 'hidden',
          }}
        >
          {session.id ? (
            <>
              <span style={{ color: 'var(--text-cream-2)' }}>session {session.id}</span>
              <span style={{ color: 'var(--text-cream-4)' }}>·</span>
            </>
          ) : null}
          <Stat value={stats.total} label="nodes" />
          <span style={{ color: 'var(--text-cream-4)' }}>·</span>
          <Stat value={stats.done} label="done" tone="var(--accent-sage)" />
          <span style={{ color: 'var(--text-cream-4)' }}>·</span>
          <Stat value={stats.running} label="running" tone="var(--accent-amber)" />
          <span style={{ color: 'var(--text-cream-4)' }}>·</span>
          <Stat
            value={stats.blocked}
            label="blocked"
            tone={stats.blocked ? 'var(--accent-copper)' : undefined}
          />
        </div>
        <div style={{ flex: 1 }} />
        <IconButton label="Zoom out" size="sm" onClick={onZoomOut}>
          <ZoomOut />
        </IconButton>
        <IconButton label="Zoom to fit" size="sm" onClick={onZoomFit}>
          <Maximize />
        </IconButton>
        <IconButton label="Zoom in" size="sm" onClick={onZoomIn}>
          <ZoomIn />
        </IconButton>
        <div style={sepStyle} />
        <IconButton
          label={inspectorOpen ? 'Collapse panel' : 'Open panel'}
          size="sm"
          active={inspectorOpen}
          onClick={onToggleInspector}
        >
          {inspectorOpen ? <ChevronRight /> : <PanelRight />}
        </IconButton>
      </div>
    </div>
  );
}
