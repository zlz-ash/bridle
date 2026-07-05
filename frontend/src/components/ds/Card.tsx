import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type CardVariant = 'panel' | 'sunk' | 'glass';

export interface CardProps extends Omit<HTMLAttributes<HTMLDivElement>, 'title'> {
  variant?: CardVariant;
  title?: ReactNode;
  headerRight?: ReactNode;
  bodyClassName?: string;
  children?: ReactNode;
}

/** Container surface. `panel` (solid dark), `sunk`, or `glass` (translucent cream overlay). */
export function Card({
  variant = 'panel',
  title,
  headerRight,
  children,
  className,
  bodyClassName,
  ...rest
}: CardProps) {
  return (
    <div className={cx('brd-card', `brd-card--${variant}`, className)} {...rest}>
      {(title || headerRight) && (
        <div className="brd-card__head">
          {title ? <h3 className="brd-card__title">{title}</h3> : null}
          {headerRight}
        </div>
      )}
      <div className={cx('brd-card__body', bodyClassName)}>{children}</div>
    </div>
  );
}
