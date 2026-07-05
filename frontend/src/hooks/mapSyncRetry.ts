export const MAP_SYNC_RETRY_BASE_MS = 1000;
export const MAP_SYNC_RETRY_MAX_MS = 30000;
export const MAP_SYNC_MAX_RETRY_ATTEMPTS = 8;

/** Exponential backoff capped for map sync retries. */
export function computeMapSyncRetryDelay(attempt: number): number {
  const safeAttempt = Math.max(0, attempt);
  return Math.min(MAP_SYNC_RETRY_BASE_MS * 2 ** safeAttempt, MAP_SYNC_RETRY_MAX_MS);
}

export type MapSyncRetryHandle = {
  schedule: (delayMs: number, callback: () => void) => number;
  cancel: (timerId: number) => void;
  cancelAll: () => void;
};

/** Create a cancellable retry scheduler (real timers; tests use fake timers). */
export function createMapSyncRetryHandle(): MapSyncRetryHandle {
  const timers = new Map<number, ReturnType<typeof setTimeout>>();
  let nextId = 1;
  return {
    schedule(delayMs, callback) {
      const id = nextId++;
      timers.set(
        id,
        setTimeout(() => {
          timers.delete(id);
          callback();
        }, delayMs),
      );
      return id;
    },
    cancel(timerId) {
      const timer = timers.get(timerId);
      if (timer !== undefined) {
        clearTimeout(timer);
        timers.delete(timerId);
      }
    },
    cancelAll() {
      for (const timer of timers.values()) {
        clearTimeout(timer);
      }
      timers.clear();
    },
  };
}
