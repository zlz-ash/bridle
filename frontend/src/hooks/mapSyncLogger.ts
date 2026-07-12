export type MapSyncLogEvent = {
  type: 'failure' | 'retry_scheduled' | 'retry_executed' | 'recovered' | 'abandoned';
  stage: 'map_sync';
  status: 'failed' | 'scheduled' | 'running' | 'succeeded' | 'abandoned';
  durationMs: number;
  projectId: string;
  fromSeq: number;
  targetSeq: number;
  reason?: string;
  attempt?: number;
  delayMs?: number;
  outcome?: string;
};

type MapSyncLogInput = Omit<MapSyncLogEvent, 'stage' | 'status' | 'durationMs'>
  & Partial<Pick<MapSyncLogEvent, 'stage' | 'status' | 'durationMs'>>;

const STATUS_BY_TYPE: Record<MapSyncLogEvent['type'], MapSyncLogEvent['status']> = {
  failure: 'failed',
  retry_scheduled: 'scheduled',
  retry_executed: 'running',
  recovered: 'succeeded',
  abandoned: 'abandoned',
};

let memorySink: MapSyncLogEvent[] | null = null;
let memorySinkLimit = 256;
const MAX_LIFECYCLE_CLOCKS = 256;
const lifecycleStartedAt = new Map<string, number>();

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
export function logMapSyncEvent(event: MapSyncLogInput): void {
  const lifecycleKey = `${event.projectId}\u0000${event.fromSeq}\u0000${event.targetSeq}`;
  const observedAt = performance.now();
  let startedAt = lifecycleStartedAt.get(lifecycleKey);
  if (startedAt === undefined) {
    if (lifecycleStartedAt.size >= MAX_LIFECYCLE_CLOCKS) {
      const oldestKey = lifecycleStartedAt.keys().next().value;
      if (oldestKey !== undefined) {
        lifecycleStartedAt.delete(oldestKey);
      }
    }
    startedAt = observedAt;
    lifecycleStartedAt.set(lifecycleKey, startedAt);
  }
  const structured: MapSyncLogEvent = {
    ...event,
    stage: event.stage ?? 'map_sync',
    status: event.status ?? STATUS_BY_TYPE[event.type],
    durationMs: Math.max(0, event.durationMs ?? Math.round(observedAt - startedAt)),
  };
  if (event.type === 'recovered' || event.type === 'abandoned') {
    lifecycleStartedAt.delete(lifecycleKey);
  }
  console.info('[project-map-sync]', structured);
  if (memorySink === null) {
    return;
  }
  memorySink.push(structured);
  if (memorySink.length > memorySinkLimit) {
    memorySink.splice(0, memorySink.length - memorySinkLimit);
  }
}

export function getMapSyncLogEvents(): readonly MapSyncLogEvent[] {
  return memorySink ?? [];
}

/** Clear active lifecycle clocks when a project sync is cancelled. */
export function clearMapSyncLogLifecycles(projectId: string): void {
  const prefix = `${projectId}\u0000`;
  for (const lifecycleKey of lifecycleStartedAt.keys()) {
    if (lifecycleKey.startsWith(prefix)) {
      lifecycleStartedAt.delete(lifecycleKey);
    }
  }
}

export function clearMapSyncLogEvents(): void {
  lifecycleStartedAt.clear();
  if (memorySink !== null) {
    memorySink.length = 0;
  }
}
