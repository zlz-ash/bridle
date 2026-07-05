export type MapSyncLogEvent = {
  type: 'failure' | 'retry_scheduled' | 'retry_executed' | 'recovered' | 'abandoned';
  projectId: string;
  fromSeq: number;
  targetSeq: number;
  reason?: string;
  attempt?: number;
  delayMs?: number;
  outcome?: string;
};

let memorySink: MapSyncLogEvent[] | null = null;
let memorySinkLimit = 256;

/** Enable a bounded in-memory sink for tests; production uses console only. */
export function configureMapSyncLogSink(
  options: { enabled: true; maxEvents?: number } | null,
): void {
  if (options?.enabled) {
    memorySink = [];
    memorySinkLimit = options.maxEvents ?? 256;
    return;
  }
  memorySink = null;
}

/** Record one map sync lifecycle event for diagnostics and tests. */
export function logMapSyncEvent(event: MapSyncLogEvent): void {
  console.info('[project-map-sync]', event);
  if (memorySink === null) {
    return;
  }
  memorySink.push(event);
  if (memorySink.length > memorySinkLimit) {
    memorySink.splice(0, memorySink.length - memorySinkLimit);
  }
}

export function getMapSyncLogEvents(): readonly MapSyncLogEvent[] {
  return memorySink ?? [];
}

export function clearMapSyncLogEvents(): void {
  if (memorySink !== null) {
    memorySink.length = 0;
  }
}
