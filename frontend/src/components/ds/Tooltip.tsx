import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export interface TooltipProps extends Omit<HTMLAttributes<HTMLSpanElement>, 'content'> {
  content: ReactNode;
  side?: 'top' | 'bottom';
  children: ReactNode;
}

/** Hover/focus tooltip. Wrap any trigger; pass the label via `content`. */
export function Tooltip({ content, side = 'top', children, className, ...rest }: TooltipProps) {
  return (
    <span className={cx('brd-tip-wrap', className)} {...rest}>
      {children}
      <span role="tooltip" className="brd-tip" data-side={side}>
        {content}
      </span>
    </span>
  );
}
