import type { HTMLAttributes, ReactNode } from 'react';
import { cx } from './cx';

export interface TabItem {
  value: string;
  label: ReactNode;
  icon?: ReactNode;
}

export interface TabsProps extends Omit<HTMLAttributes<HTMLDivElement>, 'onChange'> {
  items: ReadonlyArray<TabItem | string>;
  value: string;
  onChange: (next: string) => void;
}

/** Segmented control. Compact; for view switches in chrome. */
export function Tabs({ items, value, onChange, className, ...rest }: TabsProps) {
  return (
    <div role="tablist" className={cx('brd-tabs', className)} {...rest}>
      {items.map((it) => {
        const id = typeof it === 'string' ? it : it.value;
        const label = typeof it === 'string' ? it : it.label;
        const icon = typeof it === 'string' ? null : it.icon;
        return (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={value === id}
            className="brd-tab"
            onClick={() => onChange(id)}
          >
            {icon}
            {label}
          </button>
        );
      })}
    </div>
  );
}
