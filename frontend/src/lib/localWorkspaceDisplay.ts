import type { WorkspaceEntry } from '../components/WorkspaceSwitcher';

export const LOCAL_BACKEND_WORKSPACE_ID = 'local';
export const LOCAL_BACKEND_ENTRY_PATH = 'http://127.0.0.1:8900';

export type HealthWorkspaceSource = {
  workspace?: string | null;
};

export function resolveLocalWorkspaceDisplayPath(
  entry: Pick<WorkspaceEntry, 'id' | 'path'>,
  health: HealthWorkspaceSource | undefined,
): string {
  if (entry.id !== LOCAL_BACKEND_WORKSPACE_ID) return entry.path;
  const workspace = health?.workspace?.trim();
  return workspace || entry.path;
}

export function withHealthWorkspaceDisplay(
  workspaces: WorkspaceEntry[],
  health: HealthWorkspaceSource | undefined,
): WorkspaceEntry[] {
  return workspaces.map((ws) => (
    ws.id === LOCAL_BACKEND_WORKSPACE_ID
      ? { ...ws, path: resolveLocalWorkspaceDisplayPath(ws, health) }
      : ws
  ));
}
