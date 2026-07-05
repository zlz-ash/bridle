import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type ToastTone = 'neutral' | 'running' | 'completed' | 'failed';

export interface ToastProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  tone?: ToastTone;
  icon?: ReactNode;
  title?: ReactNode;
  children?: ReactNode;
}

/** Transient notification. Tone maps to agent state; warm-dark, slides in (no bounce). */
export function Toast({ tone = 'neutral', icon, title, children, className, ...rest }: ToastProps) {
  return (
    <div role="status" className={cx('brd-toast', `brd-toast--${tone}`, className)} {...rest}>
      <span className="brd-toast__accent" />
      {icon ? <span className="brd-toast__ic">{icon}</span> : null}
      <div className="brd-toast__body">
        {title ? <div className="brd-toast__title">{title}</div> : null}
        {children ? <div className="brd-toast__msg">{children}</div> : null}
      </div>
    </div>
  );
}
