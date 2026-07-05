import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type BadgeTone = 'neutral' | 'idle' | 'running' | 'completed' | 'failed';

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  /** Show the small leading dot. Running tone makes it blink. */
  dot?: boolean;
  /** Render in mono tabular style for numeric counts. */
  count?: boolean;
  children?: ReactNode;
}

/** Small uppercase status pill. Maps directly to agent states. */
export function Badge({ tone = 'neutral', dot = false, count = false, children, className, ...rest }: BadgeProps) {
  return (
    <span
      className={cx('brd-badge', `brd-badge--${tone}`, count && 'brd-badge--count', className)}
      {...rest}
    >
      {dot ? <span className="brd-badge-dot" /> : null}
      {children}
    </span>
  );
}
