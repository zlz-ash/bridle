import { describe, expect, it, vi } from 'vitest';
import {
  clearMapSyncLogLifecycles,
  clearMapSyncLogEvents,
  configureMapSyncLogSink,
  getMapSyncLogEvents,
  logMapSyncEvent,
} from '../mapSyncLogger';

describe('mapSyncLogger', () => {
  it('does not retain events in production mode', () => {
    configureMapSyncLogSink(null);
    logMapSyncEvent({
      type: 'failure',
      projectId: 'project-1',
      fromSeq: 1,
      targetSeq: 2,
      reason: 'test',
    });
    expect(getMapSyncLogEvents()).toHaveLength(0);
  });

  it('retains a bounded number of events in test sink mode', () => {
    configureMapSyncLogSink({ enabled: true, maxEvents: 3 });
    clearMapSyncLogEvents();
    for (let index = 0; index < 5; index += 1) {
      logMapSyncEvent({
        type: 'failure',
        projectId: 'project-1',
        fromSeq: index,
        targetSeq: index + 1,
      });
    }
    expect(getMapSyncLogEvents()).toHaveLength(3);
    expect(getMapSyncLogEvents()[0]?.fromSeq).toBe(2);
  });

  it('enriches events with structured stage duration and status', () => {
    configureMapSyncLogSink({ enabled: true });
    clearMapSyncLogEvents();

    logMapSyncEvent({
      type: 'failure',
      projectId: 'project-1',
      fromSeq: 1,
      targetSeq: 2,
      reason: 'change_apply_failed',
    });

    const [event] = getMapSyncLogEvents();
    expect(event).toMatchObject({
      stage: 'map_sync',
      status: 'failed',
    });
    expect(event?.durationMs).toEqual(expect.any(Number));
    expect(event?.durationMs).toBeGreaterThanOrEqual(0);
  });

  it('records lifecycle status and elapsed duration from a monotonic clock', () => {
    configureMapSyncLogSink({ enabled: true });
    clearMapSyncLogEvents();
    const now = vi.spyOn(performance, 'now')
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(125)
      .mockReturnValueOnce(140)
      .mockReturnValueOnce(175)
      .mockReturnValueOnce(200)
      .mockReturnValueOnce(250);

    try {
      for (const type of ['failure', 'retry_scheduled', 'retry_executed', 'recovered'] as const) {
        logMapSyncEvent({ type, projectId: 'project-1', fromSeq: 1, targetSeq: 2 });
      }
      logMapSyncEvent({ type: 'failure', projectId: 'project-1', fromSeq: 2, targetSeq: 3 });
      logMapSyncEvent({ type: 'abandoned', projectId: 'project-1', fromSeq: 2, targetSeq: 3 });
    } finally {
      now.mockRestore();
    }

    expect(getMapSyncLogEvents().map((event) => event.status)).toEqual([
      'failed',
      'scheduled',
      'running',
      'succeeded',
      'failed',
      'abandoned',
    ]);
    expect(getMapSyncLogEvents().map((event) => event.durationMs)).toEqual([0, 25, 40, 75, 0, 50]);
  });

  it('clears cancelled lifecycle clocks for only the selected project', () => {
    configureMapSyncLogSink({ enabled: true });
    clearMapSyncLogEvents();
    const now = vi.spyOn(performance, 'now')
      .mockReturnValueOnce(100)
      .mockReturnValueOnce(110)
      .mockReturnValueOnce(200)
      .mockReturnValueOnce(210);

    try {
      logMapSyncEvent({ type: 'failure', projectId: 'project-1', fromSeq: 1, targetSeq: 2 });
      logMapSyncEvent({ type: 'failure', projectId: 'project-2', fromSeq: 1, targetSeq: 2 });
      clearMapSyncLogLifecycles('project-1');
      logMapSyncEvent({ type: 'failure', projectId: 'project-1', fromSeq: 1, targetSeq: 2 });
      logMapSyncEvent({ type: 'retry_scheduled', projectId: 'project-2', fromSeq: 1, targetSeq: 2 });
    } finally {
      now.mockRestore();
    }

    expect(getMapSyncLogEvents().map((event) => event.durationMs)).toEqual([0, 0, 0, 100]);
  });

  it('bounds lifecycle clock retention independently of the event sink', () => {
    configureMapSyncLogSink({ enabled: true, maxEvents: 300 });
    clearMapSyncLogEvents();
    let observedAt = 0;
    const now = vi.spyOn(performance, 'now').mockImplementation(() => observedAt++);
    const consoleInfo = vi.spyOn(console, 'info').mockImplementation(() => undefined);

    try {
      for (let index = 0; index <= 256; index += 1) {
        logMapSyncEvent({
          type: 'failure',
          projectId: 'project-bounded',
          fromSeq: index,
          targetSeq: index + 1,
        });
      }
      logMapSyncEvent({
        type: 'retry_scheduled',
        projectId: 'project-bounded',
        fromSeq: 0,
        targetSeq: 1,
      });
    } finally {
      now.mockRestore();
      consoleInfo.mockRestore();
    }

    expect(getMapSyncLogEvents().at(-1)?.durationMs).toBe(0);
  });
});
