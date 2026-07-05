import type { InputHTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export type InputVariant = 'field' | 'pill';

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  variant?: InputVariant;
  icon?: ReactNode;
  trailing?: ReactNode;
  invalid?: boolean;
  /** Optional wrapper className (sits on the field <div>). */
  wrapperClassName?: string;
}

/** Text input. `variant="pill"` is the cream composer bar; default is a dark field. */
export function Input({
  variant = 'field',
  icon,
  trailing,
  invalid = false,
  disabled = false,
  className,
  wrapperClassName,
  ...rest
}: InputProps) {
  return (
    <div
      aria-disabled={disabled || undefined}
      className={cx(
        'brd-field',
        `brd-field--${variant}`,
        invalid && 'brd-field--invalid',
        wrapperClassName,
      )}
    >
      {icon ? <span className="brd-field-ic">{icon}</span> : null}
      <input disabled={disabled} className={className} {...rest} />
      {trailing ? <span className="brd-field-ic">{trailing}</span> : null}
    </div>
  );
}
