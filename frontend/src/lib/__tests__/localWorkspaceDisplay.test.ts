import { describe, it, expect } from 'vitest';
import {
  LOCAL_BACKEND_ENTRY_PATH,
  resolveLocalWorkspaceDisplayPath,
  withHealthWorkspaceDisplay,
} from '../localWorkspaceDisplay';

const localEntry = {
  id: 'local',
  name: 'local',
  path: LOCAL_BACKEND_ENTRY_PATH,
};

describe('resolveLocalWorkspaceDisplayPath', () => {
  it('uses health workspace for local entry when available', () => {
    expect(resolveLocalWorkspaceDisplayPath(localEntry, { workspace: 'D:\\Bridle-workspace' }))
      .toBe('D:\\Bridle-workspace');
  });

  it('falls back to backend entry path when health has no workspace', () => {
    expect(resolveLocalWorkspaceDisplayPath(localEntry, { workspace: '' }))
      .toBe(LOCAL_BACKEND_ENTRY_PATH);
    expect(resolveLocalWorkspaceDisplayPath(localEntry, undefined))
      .toBe(LOCAL_BACKEND_ENTRY_PATH);
  });

  it('does not change non-local entries', () => {
    const custom = { id: 'ws-1', name: 'demo', path: 'D:/work/demo-ws' };
    expect(resolveLocalWorkspaceDisplayPath(custom, { workspace: 'D:\\Bridle-workspace' }))
      .toBe('D:/work/demo-ws');
  });
});

describe('withHealthWorkspaceDisplay', () => {
  it('overrides only the local workspace path in the list', () => {
    const workspaces = [
      localEntry,
      { id: 'ws-1', name: 'demo', path: 'D:/work/demo-ws' },
    ];

    expect(withHealthWorkspaceDisplay(workspaces, { workspace: 'D:\\Bridle-workspace' })).toEqual([
      { id: 'local', name: 'local', path: 'D:\\Bridle-workspace' },
      { id: 'ws-1', name: 'demo', path: 'D:/work/demo-ws' },
    ]);
  });
});
