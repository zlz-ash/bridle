import { describe, expect, it } from 'vitest';
import {
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
});
