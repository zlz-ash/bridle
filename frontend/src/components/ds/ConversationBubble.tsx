import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type BubbleFrom = 'agent' | 'user';

export interface ConversationBubbleProps extends HTMLAttributes<HTMLDivElement> {
  from?: BubbleFrom;
  /** Render a blinking caret to indicate live streaming. */
  streaming?: boolean;
  children?: ReactNode;
}

/** A conversation bubble inside the overlay. `from` sets sender styling. */
export function ConversationBubble({
  from = 'agent',
  streaming = false,
  children,
  className,
  ...rest
}: ConversationBubbleProps) {
  return (
    <div className={cx('brd-bubble', `brd-bubble--${from}`, className)} {...rest}>
      {children}
      {streaming ? <span className="brd-caret" aria-hidden="true" /> : null}
    </div>
  );
}
