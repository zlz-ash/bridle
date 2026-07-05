import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pickWorkspaceDirectory } from '../workspaceDirectoryPicker';

describe('resolvePathFromDirectoryHandle recursion', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('recovers path when top level has only subdirectories', async () => {
    const nestedFile = {
      name: 'index.ts',
      path: 'D:\\work\\my-project\\src\\index.ts',
    } as File & { path: string };

    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'my-project',
      values: async function* () {
        yield {
          kind: 'directory',
          name: 'src',
          values: async function* () {
            yield { kind: 'file', getFile: async () => nestedFile };
          },
        };
      },
    }));

    expect(await pickWorkspaceDirectory()).toEqual({
      status: 'picked',
      name: 'my-project',
      path: 'D:/work/my-project',
    });
  });

  it('returns unsupported when no file in tree exposes path info', async () => {
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'empty-project',
      values: async function* () {
        yield {
          kind: 'directory',
          name: 'src',
          values: async function* () {
            yield {
              kind: 'file',
              getFile: async () => ({ name: 'index.ts' } as File),
            };
          },
        };
      },
    }));
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'unsupported' });
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it('stops scanning after first resolvable file sample', async () => {
    const secondGetFile = vi.fn(async () => ({
      name: 'second.txt',
      path: 'D:\\work\\proj\\second.txt',
    } as unknown as File));

    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'proj',
      values: async function* () {
        yield {
          kind: 'file',
          getFile: async () => ({
            name: 'first.txt',
            path: 'D:\\work\\proj\\first.txt',
          } as unknown as File),
        };
        yield { kind: 'file', getFile: secondGetFile };
      },
    }));

    await pickWorkspaceDirectory();
    expect(secondGetFile).not.toHaveBeenCalled();
  });
});
