import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { WorkspaceSwitcher } from '../WorkspaceSwitcher';
import * as picker from '../../lib/workspaceDirectoryPicker';
import type { PickWorkspaceDirectoryResult } from '../../lib/workspaceDirectoryPicker';

describe('WorkspaceSwitcher directory flow', () => {
  const workspaces = [
    { id: 'local', name: 'local', path: 'http://127.0.0.1:8900' },
  ];

  beforeEach(() => {
    vi.restoreAllMocks();
  });

  function openMenu() {
    fireEvent.click(screen.getByTitle('Switch workspace'));
  }

  it('opens directory picker when clicking new workspace', async () => {
    const pickSpy = vi.spyOn(picker, 'pickWorkspaceDirectory').mockResolvedValue({
      status: 'picked',
      name: 'bridle-workspace',
      path: 'D:/Projects/alpha/bridle-workspace',
    });

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    openMenu();
    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));

    await waitFor(() => {
      expect(pickSpy).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByDisplayValue('bridle-workspace')).toBeTruthy();
  });

  it('creates workspace from picked directory with full path', async () => {
    vi.spyOn(picker, 'pickWorkspaceDirectory').mockResolvedValue({
      status: 'picked',
      name: 'demo-ws',
      path: 'D:/work/demo-ws',
    });
    const onCreate = vi.fn();

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={onCreate}
      />,
    );

    openMenu();
    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));
    fireEvent.click(await screen.findByRole('button', { name: /^create workspace$/i }));

    expect(onCreate).toHaveBeenCalledWith({ name: 'demo-ws', path: 'D:/work/demo-ws' });
  });

  it('shows picked directory path in create preview', async () => {
    vi.spyOn(picker, 'pickWorkspaceDirectory').mockResolvedValue({
      status: 'picked',
      name: 'demo-ws',
      path: 'D:/work/demo-ws',
    });

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    openMenu();
    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));

    expect(await screen.findByText('D:/work/demo-ws')).toBeTruthy();
  });

  it('does not create workspace when picker cancelled', async () => {
    vi.spyOn(picker, 'pickWorkspaceDirectory').mockResolvedValue({ status: 'cancelled' });
    const onCreate = vi.fn();

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={onCreate}
      />,
    );

    openMenu();
    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));

    await waitFor(() => {
      expect(picker.pickWorkspaceDirectory).toHaveBeenCalled();
    });
    expect(onCreate).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /^create workspace$/i })).toBeNull();
  });

  it('keeps menu open when clicking inside menu panel', () => {
    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    openMenu();
    const menu = screen.getByText('workspaces');
    fireEvent.mouseDown(menu);
    expect(screen.getByRole('button', { name: /new workspace from local folder/i })).toBeTruthy();
  });

  it('restores button after cancel and allows retry', async () => {
    let resolvePick: ((value: PickWorkspaceDirectoryResult) => void) | undefined;
    const pickSpy = vi.spyOn(picker, 'pickWorkspaceDirectory').mockImplementation(
      () => new Promise((resolve) => { resolvePick = resolve; }),
    );

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={vi.fn()}
      />,
    );

    openMenu();
    const pickButton = screen.getByRole('button', { name: /new workspace from local folder/i });
    fireEvent.click(pickButton);

    expect(await screen.findByRole('button', { name: /opening folder picker/i })).toBeTruthy();

    resolvePick?.({ status: 'cancelled' });

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /new workspace from local folder/i })).toBeTruthy();
    });
    expect(screen.queryByRole('button', { name: /opening folder picker/i })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));
    expect(pickSpy).toHaveBeenCalledTimes(2);
  });

  it('shows unsupported message and restores button when modern picker cannot resolve path', async () => {
    vi.spyOn(picker, 'pickWorkspaceDirectory').mockResolvedValue({ status: 'unsupported' });
    const onCreate = vi.fn();

    render(
      <WorkspaceSwitcher
        workspaces={workspaces}
        activeId="local"
        onSwitch={vi.fn()}
        onCreate={onCreate}
      />,
    );

    openMenu();
    fireEvent.click(screen.getByRole('button', { name: /new workspace from local folder/i }));

    expect(await screen.findByText(/does not support persisting local folder workspaces/i)).toBeTruthy();
    expect(onCreate).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /^create workspace$/i })).toBeNull();
    expect(screen.getByRole('button', { name: /new workspace from local folder/i }).hasAttribute('disabled')).toBe(false);
  });
});
