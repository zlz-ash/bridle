import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual emphasis. Default 'secondary'. Use 'primary' (amber) for the single main action. */
  variant?: ButtonVariant;
  /** Control height. Default 'md' (36px). */
  size?: ButtonSize;
  /** Leading icon node (16px Lucide outline recommended). */
  icon?: ReactNode;
  /** Trailing icon node. */
  iconRight?: ReactNode;
  children?: ReactNode;
}

/** Bridle action button. Warm-dark, sentence-case, no scale-on-press. */
export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  iconRight,
  children,
  className,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={cx(
        'brd-btn',
        `brd-btn--${variant}`,
        size !== 'md' && `brd-btn--${size}`,
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
      {iconRight}
    </button>
  );
}
