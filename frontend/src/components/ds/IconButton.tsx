import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  size?: 'sm' | 'md';
  solid?: boolean;
  active?: boolean;
  /** Accessible label; also used for the title tooltip. */
  label?: string;
  children?: ReactNode;
}

/** Square icon-only button for toolbars and chrome. */
export function IconButton({
  size = 'md',
  solid = false,
  active = false,
  label,
  children,
  className,
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      aria-label={label}
      title={label}
      className={cx(
        'brd-iconbtn',
        size === 'sm' && 'brd-iconbtn--sm',
        solid && 'brd-iconbtn--solid',
        active && 'brd-iconbtn--active',
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  );
}
