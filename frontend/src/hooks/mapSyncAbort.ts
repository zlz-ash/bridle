import { isAxiosError } from 'axios';

/** True when an error represents an intentional sync cancellation. */
export function isSyncAbortError(error: unknown): boolean {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return true;
  }
  if (isAxiosError(error) && (error.code === 'ERR_CANCELED' || error.name === 'CanceledError')) {
    return true;
  }
  return false;
}

/** Throw AbortError when the sync signal has fired. */
export function throwIfAborted(signal?: AbortSignal): void {
  if (signal?.aborted) {
    throw new DOMException('Aborted', 'AbortError');
  }
}

/** Run an async fetch; reject with AbortError if the signal fires while pending. */
export function runWithAbortSignal<T>(signal: AbortSignal, run: () => Promise<T>): Promise<T> {
  throwIfAborted(signal);
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(new DOMException('Aborted', 'AbortError'));
    signal.addEventListener('abort', onAbort);
    run()
      .then((value) => {
        signal.removeEventListener('abort', onAbort);
        throwIfAborted(signal);
        resolve(value);
      })
      .catch((error) => {
        signal.removeEventListener('abort', onAbort);
        reject(error);
      });
  });
}
