import type { ButtonHTMLAttributes } from 'react';
import { cx } from './cx';

export interface SwitchProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onChange' | 'children'> {
  checked?: boolean;
  onChange?: (next: boolean) => void;
  label?: string;
}

/** Binary toggle. Amber when on; smooth slide, no bounce. */
export function Switch({ checked = false, onChange, disabled = false, label, className, ...rest }: SwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => !disabled && onChange?.(!checked)}
      className={cx('brd-switch', className)}
      {...rest}
    >
      <span className="brd-knob" />
    </button>
  );
}
