export function LogoMark({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
         stroke="var(--ink)" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 3.5 L7.5 9 M19 3.5 L16.5 9" />
      <path d="M5 3.5 Q12 6 19 3.5" />
      <circle cx="12" cy="13.4" r="4.4" />
      <path d="M7.6 9 Q12 11 16.4 9" />
      <path d="M9 17.4 Q12 19.4 15 17.4" />
      <circle cx="12" cy="13.4" r="1" fill="var(--ink)" stroke="none" />
    </svg>
  );
}

export function DoorIcon({ open = false }: { open?: boolean }) {
  return (
    <svg width="17" height="17" viewBox="0 0 18 18" fill="none"
         stroke="currentColor" strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2.5" y="2.5" width="13" height="13" />
      <line x1={open ? 6.5 : 11.5} y1="2.5" x2={open ? 6.5 : 11.5} y2="15.5" />
      <circle cx={open ? 5.1 : 12.9} cy="9" r="0.7" fill="currentColor" stroke="none" />
    </svg>
  );
}

export const PlusIcon = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.4" strokeLinecap="round"><line x1="7" y1="2" x2="7" y2="12"/><line x1="2" y1="7" x2="12" y2="7"/></svg>
);

export const MinusIcon = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.4" strokeLinecap="round"><line x1="2" y1="7" x2="12" y2="7"/></svg>
);

export const ZoomIn = () => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round"><circle cx="7" cy="7" r="4.2"/><line x1="10.2" y1="10.2" x2="13.5" y2="13.5"/><line x1="7" y1="5.2" x2="7" y2="8.8"/><line x1="5.2" y1="7" x2="8.8" y2="7"/></svg>
);

export const ZoomOut = () => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round"><circle cx="7" cy="7" r="4.2"/><line x1="10.2" y1="10.2" x2="13.5" y2="13.5"/><line x1="5.2" y1="7" x2="8.8" y2="7"/></svg>
);

export const FitIcon = () => (
  <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
    <path d="M2 5 V2 H5 M11 2 H14 V5 M14 11 V14 H11 M5 14 H2 V11"/></svg>
);

export const GearIcon = () => (
  <svg width="16" height="16" viewBox="0 0 18 18" fill="none" stroke="currentColor"
       strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="9" cy="9" r="2.4"/>
    <path d="M9 1.6v2 M9 14.4v2 M1.6 9h2 M14.4 9h2 M3.8 3.8l1.4 1.4 M12.8 12.8l1.4 1.4 M14.2 3.8l-1.4 1.4 M5.2 12.8l-1.4 1.4"/></svg>
);

export const RerunIcon = () => (
  <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 7a5 5 0 1 1-1.5-3.5"/><path d="M12 1.5V4H9.5"/></svg>
);

export const StopIcon = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor"
       strokeWidth="1.3"><rect x="3" y="3" width="6" height="6"/></svg>
);

export const FileGlyph = () => (
  <svg width="12" height="12" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 1.5h5l3 3v8H3z"/><path d="M8 1.5V4.5h3"/></svg>
);

export const SendIcon = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round">
    <line x1="7" y1="11.5" x2="7" y2="3"/><path d="M3.5 6.5 L7 3 L10.5 6.5"/></svg>
);

export const ChevronIcon = () => (
  <svg width="10" height="10" viewBox="0 0 12 12" fill="none" stroke="currentColor"
       strokeWidth="1.3" strokeLinecap="round" strokeLinejoin="round"><path d="M3 4.5 L6 7.5 L9 4.5"/></svg>
);

export const CheckIcon = () => (
  <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="currentColor"
       strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M2.5 6.4 L5 9 L9.5 3.4"/></svg>
);

export const FolderIcon = () => (
  <svg width="13" height="13" viewBox="0 0 14 14" fill="none" stroke="currentColor"
       strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M1.5 3.5 h3.5 l1 1.2 h6 v6.3 h-10.5 z"/></svg>
);
