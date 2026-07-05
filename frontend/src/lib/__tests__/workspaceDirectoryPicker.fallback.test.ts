import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pickWorkspaceDirectory } from '../workspaceDirectoryPicker';

describe('pickWorkspaceDirectory modern picker control flow', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns picked directly when modern picker resolves a real path', async () => {
    const file = {
      name: 'note.txt',
      path: 'D:\\work\\demo-ws\\note.txt',
    } as File & { path: string };
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'demo-ws',
      values: async function* () {
        yield { kind: 'file', getFile: async () => file };
      },
    }));

    expect(await pickWorkspaceDirectory()).toEqual({
      status: 'picked',
      name: 'demo-ws',
      path: 'D:/work/demo-ws',
    });
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it('returns unsupported when modern picker cannot resolve path without opening input fallback', async () => {
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'demo-ws',
      values: async function* () {},
    }));
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    await expect(pickWorkspaceDirectory()).resolves.toEqual({ status: 'unsupported' });
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it('settles immediately when modern picker cannot resolve path', async () => {
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'demo-ws',
      values: async function* () {},
    }));
    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(() => {
      /* simulate blocked fallback that never fires change/focus */
    });

    const hang = new Promise((_resolve, reject) => {
      setTimeout(() => reject(new Error('pickWorkspaceDirectory hung')), 100);
    });

    await expect(Promise.race([pickWorkspaceDirectory(), hang])).resolves.toEqual({
      status: 'unsupported',
    });
  });

  it('returns cancelled when modern picker is cancelled without opening input fallback', async () => {
    vi.stubGlobal(
      'showDirectoryPicker',
      vi.fn().mockRejectedValue(Object.assign(new Error('cancelled'), { name: 'AbortError' })),
    );
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'cancelled' });
    expect(clickSpy).not.toHaveBeenCalled();
  });
});
