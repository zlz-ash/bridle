import type { CSSProperties, HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type AgentNodeState = 'idle' | 'running' | 'completed' | 'failed';

export interface AgentNodeProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  state?: AgentNodeState;
  title?: ReactNode;
  meta?: ReactNode;
  icon?: ReactNode;
  width?: number | string;
  /** Lift above the conversation overlay using `mix-blend-mode: screen`. */
  punchThrough?: boolean;
  selected?: boolean;
}

/** A flowchart node = one sub-agent. State drives color + the alive pulse. */
export function AgentNode({
  state = 'idle',
  title,
  meta,
  icon,
  width,
  punchThrough = false,
  selected = false,
  onClick,
  className,
  style,
  ...rest
}: AgentNodeProps) {
  const interactive = !!onClick;
  const widthVar: CSSProperties | undefined = width
    ? ({ ['--brd-node-w' as string]: typeof width === 'number' ? `${width}px` : width } as CSSProperties)
    : undefined;
  return (
    <div
      role={interactive ? 'button' : undefined}
      tabIndex={interactive ? 0 : undefined}
      onClick={onClick}
      className={cx(
        'brd-node',
        `brd-node--${state}`,
        punchThrough && 'brd-node--punch',
        selected && 'brd-node--selected',
        className,
      )}
      style={{ ...widthVar, ...style }}
      {...rest}
    >
      <div className="brd-node__top">
        <span className="brd-node__dot" />
        <span className="brd-node__title">{title}</span>
        {icon ? <span className="brd-node__ic">{icon}</span> : null}
      </div>
      {meta ? <div className="brd-node__meta">{meta}</div> : null}
    </div>
  );
}
