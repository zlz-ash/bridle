import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
  extractDirectoryPathFromFiles,
  pickWorkspaceDirectory,
} from '../workspaceDirectoryPicker';

function mockFileList(file: File & { path?: string; webkitRelativePath?: string }): FileList {
  return {
    length: 1,
    item: (index: number) => (index === 0 ? file : null),
    0: file,
  } as unknown as FileList;
}

describe('extractDirectoryPathFromFiles', () => {
  it('derives absolute directory path from file.path and webkitRelativePath', () => {
    const file = {
      name: 'readme.txt',
      path: 'D:\\Projects\\alpha\\bridle-workspace\\readme.txt',
      webkitRelativePath: 'bridle-workspace/readme.txt',
    } as File & { path: string; webkitRelativePath: string };

    expect(extractDirectoryPathFromFiles(mockFileList(file))).toBe('D:/Projects/alpha/bridle-workspace');
  });

  it('distinguishes same-named directories under different parents', () => {
    const mk = (full: string, rel: string) => {
      const file = {
        name: 'f.txt',
        path: full,
        webkitRelativePath: rel,
      } as File & { path: string; webkitRelativePath: string };
      return extractDirectoryPathFromFiles(mockFileList(file));
    };

    const a = mk('D:\\a\\bridle-workspace\\f.txt', 'bridle-workspace/f.txt');
    const b = mk('D:\\b\\bridle-workspace\\f.txt', 'bridle-workspace/f.txt');
    expect(a).not.toBe(b);
    expect(a).toBe('D:/a/bridle-workspace');
    expect(b).toBe('D:/b/bridle-workspace');
  });
});

describe('pickWorkspaceDirectory', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns absolute path from showDirectoryPicker when handle scan resolves one', async () => {
    const file = {
      name: 'note.txt',
      path: 'D:\\work\\demo-ws\\note.txt',
      webkitRelativePath: 'demo-ws/note.txt',
    } as File & { path: string; webkitRelativePath: string };
    const handle = {
      name: 'demo-ws',
      values: async function* () {
        yield {
          kind: 'file',
          getFile: async () => file,
        };
      },
    };
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue(handle));

    const result = await pickWorkspaceDirectory();

    expect(result).toEqual({
      status: 'picked',
      name: 'demo-ws',
      path: 'D:/work/demo-ws',
    });
  });

  it('returns cancelled when user cancels showDirectoryPicker', async () => {
    vi.stubGlobal(
      'showDirectoryPicker',
      vi.fn().mockRejectedValue(Object.assign(new Error('cancelled'), { name: 'AbortError' })),
    );

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'cancelled' });
  });
});

describe('pickWorkspaceDirectory webkitdirectory fallback', () => {
  it('uses absolute path from selected files when showDirectoryPicker unavailable', async () => {
    vi.stubGlobal('showDirectoryPicker', undefined);

    const file = {
      name: 'f.txt',
      path: 'D:\\root\\alpha\\demo-ws\\f.txt',
      webkitRelativePath: 'demo-ws/f.txt',
    } as File & { path: string; webkitRelativePath: string };

    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function click(this: HTMLInputElement) {
      Object.defineProperty(this, 'files', {
        configurable: true,
        value: mockFileList(file),
      });
      this.dispatchEvent(new Event('change'));
    });

    const result = await pickWorkspaceDirectory();

    clickSpy.mockRestore();
    expect(result).toEqual({
      status: 'picked',
      name: 'demo-ws',
      path: 'D:/root/alpha/demo-ws',
    });
  });
});
