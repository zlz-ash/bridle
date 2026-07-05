import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pickWorkspaceDirectory } from '../workspaceDirectoryPicker';

describe('pickWorkspaceDirectory', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns null when user cancels', async () => {
    vi.stubGlobal(
      'showDirectoryPicker',
      vi.fn().mockRejectedValue(Object.assign(new Error('cancelled'), { name: 'AbortError' })),
    );

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'cancelled' });
  });
});
