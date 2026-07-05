import { useEffect, useRef, useState } from 'react';
import { ChevronIcon, PlusIcon, CheckIcon, FolderIcon } from './Icons';
import { pickWorkspaceDirectory } from '../lib/workspaceDirectoryPicker';

export interface WorkspaceEntry {
  id: string;
  name: string;
  path: string;
}

interface Props {
  workspaces: WorkspaceEntry[];
  activeId: string | null;
  onSwitch: (id: string) => void;
  onCreate: (ws: { name: string; path: string }) => void;
}

/** Render project selection; workspace inputs exit through switch/create callbacks. */
export function WorkspaceSwitcher({ workspaces, activeId, onSwitch, onCreate }: Props) {
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<{ folderName: string; path: string } | null>(null);
  const [name, setName] = useState('');
  const [picking, setPicking] = useState(false);
  const [pickNotice, setPickNotice] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);
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

  /** Keep menu clicks local; event input exits without closing the popover. */
  const stopMenuEvent = (e: React.SyntheticEvent) => {
    e.stopPropagation();
  };

  /** Ask the browser for a directory; click input exits with a picked folder or an unsupported notice. */
  const pickDirectory = async (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (picking) return;
    setPicking(true);
    setPickNotice(null);
    try {
      const selected = await pickWorkspaceDirectory();
      if (selected.status === 'picked') {
        setPicked({ folderName: selected.name, path: selected.path });
        setName(selected.name);
        return;
      }
      if (selected.status === 'unsupported') {
        setPickNotice('This browser does not support persisting local folder workspaces.');
      }
    } finally {
      setPicking(false);
    }
  };

  /** Create a registered project; picked folder input exits after resetting the menu state. */
  const create = () => {
    if (!picked) return;
    onCreate({ name: name.trim() || picked.folderName, path: picked.path });
    setPicked(null);
    setName('');
    setOpen(false);
  };

  return (
    <div className="ws-switch" ref={rootRef}>
      <button
        className={'ws-trigger' + (open ? ' open' : '')}
        onClick={() => setOpen((o) => !o)}
        title={cur ? 'Switch workspace' : 'Open project'}
      >
        <span className="ws-dot" />
        <span className="ws-path">{cur?.path ?? 'Open project'}</span>
        <span className="ws-chev"><ChevronIcon /></span>
      </button>

      {open && (
        <div
          className="ws-menu"
          onMouseDown={stopMenuEvent}
          onClick={stopMenuEvent}
        >
          <div className="ws-menu-label">workspaces</div>
          <div className="ws-list">
            {workspaces.map((w) => (
              <button
                key={w.id}
                type="button"
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
            <>
              {pickNotice && (
                <div className="ws-pick-notice" role="status">{pickNotice}</div>
              )}
              <button
              type="button"
              className="ws-new"
              disabled={picking}
              onMouseDown={stopMenuEvent}
              onClick={(e) => { void pickDirectory(e); }}
            >
              <PlusIcon /> {picking ? 'Opening folder picker…' : 'New workspace from local folder…'}
            </button>
            </>
          ) : (
            <div className="ws-create">
              <div className="ws-create-file"><FolderIcon /> {picked.path}</div>
              <input
                className="ws-create-name"
                value={name}
                autoFocus
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && create()}
                placeholder="workspace name"
              />
              <div className="ws-create-actions">
                <button type="button" className="ws-cancel" onClick={() => setPicked(null)}>Cancel</button>
                <button type="button" className="ws-confirm" onClick={create}>Create workspace</button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
