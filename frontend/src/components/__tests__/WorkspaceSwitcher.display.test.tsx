import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { WorkspaceSwitcher } from '../WorkspaceSwitcher';
import { LOCAL_BACKEND_ENTRY_PATH } from '../../lib/localWorkspaceDisplay';

describe('WorkspaceSwitcher local path display', () => {
  it('shows health workspace path in trigger and dropdown list for local entry', () => {
    const workspaces = [
      { id: 'local', name: 'local', path: 'D:\\Bridle-workspace' },
    ];

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={() => {}}
        onCreate={() => {}}
      />,
    );

    expect(screen.getByTitle('Switch workspace').textContent).toContain('D:\\Bridle-workspace');

    fireEvent.click(screen.getByTitle('Switch workspace'));

    const paths = screen.getAllByText('D:\\Bridle-workspace');
    expect(paths).toHaveLength(2);
    expect(document.querySelector('.ws-path')?.textContent).toBe('D:\\Bridle-workspace');
    expect(document.querySelector('.ws-item-path')?.textContent).toBe('D:\\Bridle-workspace');
    expect(screen.queryByText(LOCAL_BACKEND_ENTRY_PATH)).toBeNull();
  });

  it('shows backend entry path when health workspace is unavailable', () => {
    const workspaces = [
      { id: 'local', name: 'local', path: LOCAL_BACKEND_ENTRY_PATH },
    ];

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={() => {}}
        onCreate={() => {}}
      />,
    );

    expect(screen.getByTitle('Switch workspace').textContent).toContain(LOCAL_BACKEND_ENTRY_PATH);
  });
});
