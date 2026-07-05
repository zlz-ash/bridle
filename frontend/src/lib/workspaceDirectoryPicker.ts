export type PickedWorkspaceDirectory = {
  name: string;
  path: string;
};

export type PickWorkspaceDirectoryResult =
  | ({ status: 'picked' } & PickedWorkspaceDirectory)
  | { status: 'cancelled' }
  | { status: 'unsupported' };

type DirectoryEntryLike = {
  kind: string;
  name?: string;
  getFile?: () => Promise<File>;
  values?: () => AsyncIterable<DirectoryEntryLike>;
};

type DirectoryHandleLike = {
  name: string;
  values?: () => AsyncIterable<DirectoryEntryLike>;
};

export const INPUT_PICKER_FOCUS_GRACE_MS = 500;
export const MAX_DIRECTORY_SCAN_DEPTH = 8;

export function normalizeDirectoryPath(path: string): string {
  return path.replace(/\\/g, '/');
}

export function extractDirectoryPathFromFiles(files: FileList | null | undefined): string | null {
  if (!files || files.length === 0) return null;
  const first = files[0] as File & { path?: string; webkitRelativePath?: string };
  const rawPath = first.path;
  if (!rawPath) return null;

  const filePath = normalizeDirectoryPath(rawPath);
  const rel = normalizeDirectoryPath(first.webkitRelativePath || first.name);

  if (rel.includes('/')) {
    const firstSegment = rel.split('/')[0] ?? '';
    const suffix = rel.slice(firstSegment.length + 1);
    if (suffix && filePath.endsWith(`/${suffix}`)) {
      return filePath.slice(0, -(suffix.length + 1));
    }
  }

  const slash = filePath.lastIndexOf('/');
  return slash > 0 ? filePath.slice(0, slash) : null;
}

export function buildPickedDirectory(name: string, absolutePath: string | null): PickWorkspaceDirectoryResult {
  const trimmedName = name.trim() || 'workspace';
  if (!absolutePath) return { status: 'unsupported' };
  return {
    status: 'picked',
    name: trimmedName,
    path: normalizeDirectoryPath(absolutePath),
  };
}

async function resolvePathFromDirectoryHandle(
  handle: DirectoryHandleLike,
  rootName: string,
  relativePath = '',
  depth = 0,
): Promise<string | null> {
  if (!handle.values || depth > MAX_DIRECTORY_SCAN_DEPTH) return null;

  for await (const entry of handle.values()) {
    if (entry.kind === 'file' && entry.getFile) {
      const file = await entry.getFile() as File & { path?: string; webkitRelativePath?: string };
      const fileRelSuffix = relativePath ? `${relativePath}/${file.name}` : file.name;
      const rel = `${rootName}/${fileRelSuffix}`;
      Object.defineProperty(file, 'webkitRelativePath', {
        value: rel,
        configurable: true,
      });
      const dt = { 0: file, length: 1, item: (i: number) => (i === 0 ? file : null) } as unknown as FileList;
      const resolved = extractDirectoryPathFromFiles(dt);
      if (resolved) return resolved;
      continue;
    }

    if (entry.kind === 'directory') {
      const dirName = entry.name ?? 'dir';
      const nestedPath = relativePath ? `${relativePath}/${dirName}` : dirName;
      const nested = await resolvePathFromDirectoryHandle(
        { name: dirName, values: entry.values },
        rootName,
        nestedPath,
        depth + 1,
      );
      if (nested) return nested;
    }
  }

  return null;
}

export async function pickWorkspaceDirectory(): Promise<PickWorkspaceDirectoryResult> {
  if (typeof window === 'undefined') return { status: 'cancelled' };

  const picker = (window as Window & {
    showDirectoryPicker?: () => Promise<DirectoryHandleLike>;
  }).showDirectoryPicker;

  if (picker) {
    try {
      const handle = await picker.call(window);
      const name = handle.name.trim() || 'workspace';
      const absolutePath = await resolvePathFromDirectoryHandle(handle, name);
      if (absolutePath) {
        return buildPickedDirectory(name, absolutePath);
      }
      return { status: 'unsupported' };
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') return { status: 'cancelled' };
      if (err instanceof Error && err.name === 'AbortError') return { status: 'cancelled' };
      throw err;
    }
  }

  return pickWorkspaceDirectoryViaInput();
}

function pickWorkspaceDirectoryViaInput(): Promise<PickWorkspaceDirectoryResult> {
  return new Promise((resolve) => {
    let settled = false;
    const input = document.createElement('input');
    input.type = 'file';
    input.style.display = 'none';
    input.setAttribute('webkitdirectory', '');
    input.setAttribute('directory', '');

    const finish = (result: PickWorkspaceDirectoryResult) => {
      if (settled) return;
      settled = true;
      window.removeEventListener('focus', onWindowFocus);
      input.remove();
      resolve(result);
    };

    const onWindowFocus = () => {
      window.setTimeout(() => {
        if (settled) return;
        if (!input.files || input.files.length === 0) {
          finish({ status: 'cancelled' });
        }
      }, INPUT_PICKER_FOCUS_GRACE_MS);
    };

    input.addEventListener('change', () => {
      const files = input.files;
      if (!files || files.length === 0) {
        finish({ status: 'cancelled' });
        return;
      }
      const absolutePath = extractDirectoryPathFromFiles(files);
      const first = files[0] as File & { webkitRelativePath?: string };
      const rel = first.webkitRelativePath || first.name;
      const folderName = rel.split('/')[0] || first.name;
      const name = folderName.trim() || 'workspace';
      finish(buildPickedDirectory(name, absolutePath));
    });

    window.addEventListener('focus', onWindowFocus);
    document.body.appendChild(input);
    input.click();
  });
}
