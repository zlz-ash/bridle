import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { pickWorkspaceDirectory } from '../workspaceDirectoryPicker';

function mockFileList(file: File & { path?: string; webkitRelativePath?: string }): FileList {
  return {
    length: 1,
    item: (index: number) => (index === 0 ? file : null),
    0: file,
  } as unknown as FileList;
}

describe('pickWorkspaceDirectory real path policy', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('returns unsupported when modern picker cannot resolve path', async () => {
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'bridle-workspace',
      values: async function* () {},
    }));
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'unsupported' });
    expect(clickSpy).not.toHaveBeenCalled();
  });

  it('returns picked with real path from showDirectoryPicker when resolvable', async () => {
    const file = {
      name: 'note.txt',
      path: 'D:\\work\\demo-ws\\note.txt',
      webkitRelativePath: 'demo-ws/note.txt',
    } as File & { path: string; webkitRelativePath: string };
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
  });

  it('returns unsupported when webkitdirectory files lack absolute path', async () => {
    vi.stubGlobal('showDirectoryPicker', undefined);
    const file = {
      name: 'f.txt',
      webkitRelativePath: 'demo-ws/f.txt',
    } as File & { webkitRelativePath: string };

    vi.spyOn(HTMLInputElement.prototype, 'click').mockImplementation(function click(this: HTMLInputElement) {
      Object.defineProperty(this, 'files', {
        configurable: true,
        value: mockFileList(file),
      });
      this.dispatchEvent(new Event('change'));
    });

    expect(await pickWorkspaceDirectory()).toEqual({ status: 'unsupported' });
  });

  it('never returns local-dir prefixed paths when modern picker cannot resolve path', async () => {
    vi.stubGlobal('showDirectoryPicker', vi.fn().mockResolvedValue({
      name: 'demo-ws',
      values: async function* () {},
    }));
    const clickSpy = vi.spyOn(HTMLInputElement.prototype, 'click');

    const result = await pickWorkspaceDirectory();
    expect(result.status).toBe('unsupported');
    expect(JSON.stringify(result)).not.toMatch(/local-dir:/);
    expect(clickSpy).not.toHaveBeenCalled();
  });
});
