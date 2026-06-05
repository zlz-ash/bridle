import { useEffect, useRef, useState } from 'react';
import { ChevronIcon, PlusIcon, CheckIcon, FolderIcon } from './Icons';

export interface WorkspaceEntry {
  id: string;
  name: string;
  path: string;
}

interface Props {
  workspaces: WorkspaceEntry[];
  activeId: string;
  onSwitch: (id: string) => void;
  onCreate: (ws: { name: string; path: string }) => void;
}

export function WorkspaceSwitcher({ workspaces, activeId, onSwitch, onCreate }: Props) {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<{ fileName: string; path: string } | null>(null);
  const [name, setName] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const cur = workspaces.find((w) => w.id === activeId) || workspaces[0];

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setPicked(null);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  const pickFile = () => fileRef.current?.click();
  const onFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;
    const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
    const base = rel.split('/')[0] || f.name;
    setPicked({ fileName: f.name, path: 'D:\\' + base });
    setName(base.replace(/\.[^.]+$/, '') || 'workspace');
    e.target.value = '';
  };

  const create = () => {
    if (!picked) return;
    onCreate({ name: name.trim() || 'workspace', path: picked.path });
    setPicked(null);
    setName('');
    setOpen(false);
  };

  if (!cur) return null;

  return (
    <div className="ws-switch" ref={rootRef}>
      <button
        className={'ws-trigger' + (open ? ' open' : '')}
        onClick={() => setOpen((o) => !o)}
        title="Switch workspace"
      >
        <span className="ws-dot" />
        <span className="ws-path">{cur.path}</span>
        <span className="ws-chev"><ChevronIcon /></span>
      </button>

      {open && (
        <div className="ws-menu">
          <div className="ws-menu-label">workspaces</div>
          <div className="ws-list">
            {workspaces.map((w) => (
              <button
                key={w.id}
                className={'ws-item' + (w.id === activeId ? ' active' : '')}
                onClick={() => { onSwitch(w.id); setOpen(false); }}
              >
                <span className="ws-item-main">
                  <span className="ws-item-name">{w.name}</span>
                  <span className="ws-item-path">{w.path}</span>
                </span>
                {w.id === activeId && <span className="ws-check"><CheckIcon /></span>}
              </button>
            ))}
          </div>

          {!picked ? (
            <button className="ws-new" onClick={pickFile}>
              <PlusIcon /> New workspace from local file…
            </button>
          ) : (
            <div className="ws-create">
              <div className="ws-create-file"><FolderIcon /> {picked.fileName}</div>
              <input
                className="ws-create-name"
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && create()}
                placeholder="workspace name"
              />
              <div className="ws-create-actions">
                <button className="ws-cancel" onClick={() => setPicked(null)}>Cancel</button>
                <button className="ws-confirm" onClick={create}>Create workspace</button>
              </div>
            </div>
          )}
          <input ref={fileRef} type="file" style={{ display: 'none' }} onChange={onFile} />
        </div>
      )}
    </div>
  );
}
