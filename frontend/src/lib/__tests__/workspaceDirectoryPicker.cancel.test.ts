import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pickWorkspaceDirectory } from '../workspaceDirectoryPicker';

describe('pickWorkspaceDirectory input fallback cancel', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it('resolves cancelled when dialog closes without change event', async () => {
    vi.stubGlobal('showDirectoryPicker', undefined);
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function click(this: HTMLInputElement) {
      window.dispatchEvent(new Event('focus'));
    });

    const promise = pickWorkspaceDirectory();
    await vi.advanceTimersByTimeAsync(500);
    const result = await promise;

    expect(result).toEqual({ status: 'cancelled' });
  });

  it('does not leave promise pending after cancel', async () => {
    vi.stubGlobal('showDirectoryPicker', undefined);
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function click() {
      window.dispatchEvent(new Event('focus'));
    });

    const settled = vi.fn();
    void pickWorkspaceDirectory().then(settled);
    await vi.advanceTimersByTimeAsync(500);

    expect(settled).toHaveBeenCalledWith({ status: 'cancelled' });
  });
});
