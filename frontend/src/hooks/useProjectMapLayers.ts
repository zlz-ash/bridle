import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { projectMapApi } from '../api/endpoints';
import type {
  BlindSpot,
  BoundaryOverview,
  CodeEntity,
  CodeRelation,
  InterfaceMockArtifact,
  ModuleCandidate,
  ModuleInterfaceCandidate,
  SemanticAnnotation,
} from '../api/types';
import {
  applyPostSyncUpdates,
  attemptMapLayerSync,
  MAP_LAYER_QUERY_KEYS,
} from './mapLayerSync';
import { isSyncAbortError } from './mapSyncAbort';
import { clearMapSyncLogLifecycles, logMapSyncEvent } from './mapSyncLogger';
import {
  computeMapSyncRetryDelay,
  createMapSyncRetryHandle,
  MAP_SYNC_MAX_RETRY_ATTEMPTS,
} from './mapSyncRetry';
import {
  ENTITY_CAP,
  fetchAllPages,
  MAX_RENDER_NODES,
  OVERVIEW_POLL_MS,
  PAGE_SIZE,
} from './projectMapPaging';

/** Load semantic map layers with incremental change application and render limits. */
export function useProjectMapLayers(projectId: string | null) {
  const queryClient = useQueryClient();
  const lastSeqRef = useRef(0);
  const syncGenerationRef = useRef(0);
  const retryAttemptRef = useRef(0);
  const retryHandleRef = useRef(createMapSyncRetryHandle());
  const activeRetryTimerRef = useRef<number | null>(null);
  const syncAbortRef = useRef<AbortController | null>(null);
  const projectIdRef = useRef<string | null>(projectId);
  projectIdRef.current = projectId;

  const cancelMapLayerQueries = (targetProjectId: string) =>
    Promise.all(
      MAP_LAYER_QUERY_KEYS.map((key) =>
        queryClient.cancelQueries({ queryKey: [key, targetProjectId] }),
      ),
    );

  const cancelInFlightSync = (targetProjectId?: string | null) => {
    const pid = targetProjectId ?? projectIdRef.current;
    if (pid) {
      void cancelMapLayerQueries(pid);
      clearMapSyncLogLifecycles(pid);
    }
    syncAbortRef.current?.abort();
    syncAbortRef.current = null;
    syncGenerationRef.current += 1;
    retryHandleRef.current.cancelAll();
    activeRetryTimerRef.current = null;
  };

  const [renderLimit, setRenderLimit] = useState(MAX_RENDER_NODES);
  const [fetchFallbackReason, setFetchFallbackReason] = useState<string | null>(null);
  const [syncRetryTick, setSyncRetryTick] = useState(0);

  const overviewQuery = useQuery({
    queryKey: ['project-map-overview', projectId],
    queryFn: () => projectMapApi.overview(projectId!),
    enabled: projectId !== null,
    retry: false,
    refetchInterval: OVERVIEW_POLL_MS,
  });

  useEffect(() => {
    if (!projectId || overviewQuery.data == null) return;
    const targetSeq = overviewQuery.data.change_seq;
    if (lastSeqRef.current === 0) {
      lastSeqRef.current = targetSeq;
      return;
    }
    if (targetSeq <= lastSeqRef.current) return;

    syncGenerationRef.current += 1;
    const generation = syncGenerationRef.current;
    const capturedProjectId = projectId;
    const fromSeq = lastSeqRef.current;
    const abortController = new AbortController();
    syncAbortRef.current?.abort();
    syncAbortRef.current = abortController;
    const signal = abortController.signal;

    const scheduleRetry = (reason: string) => {
      if (signal.aborted || syncGenerationRef.current !== generation || projectId !== capturedProjectId) {
        return;
      }
      const attempt = retryAttemptRef.current;
      if (attempt >= MAP_SYNC_MAX_RETRY_ATTEMPTS) {
        logMapSyncEvent({
          type: 'abandoned',
          projectId: capturedProjectId,
          fromSeq,
          targetSeq,
          reason,
          attempt,
        });
        return;
      }
      const delayMs = computeMapSyncRetryDelay(attempt);
      retryAttemptRef.current = attempt + 1;
      logMapSyncEvent({
        type: 'retry_scheduled',
        projectId: capturedProjectId,
        fromSeq,
        targetSeq,
        reason,
        attempt,
        delayMs,
      });
      if (activeRetryTimerRef.current !== null) {
        retryHandleRef.current.cancel(activeRetryTimerRef.current);
      }
      activeRetryTimerRef.current = retryHandleRef.current.schedule(delayMs, () => {
        activeRetryTimerRef.current = null;
        if (signal.aborted || syncGenerationRef.current !== generation || projectId !== capturedProjectId) {
          return;
        }
        logMapSyncEvent({
          type: 'retry_executed',
          projectId: capturedProjectId,
          fromSeq,
          targetSeq,
          reason,
          attempt,
          delayMs,
        });
        setSyncRetryTick((tick) => tick + 1);
      });
    };

    void (async () => {
      try {
        const syncResult = await attemptMapLayerSync({
          projectId: capturedProjectId,
          fromSeq,
          targetSeq,
          queryClient,
          signal,
          deps: {
            fetchChanges: (afterSeq, limit, requestSignal) =>
              projectMapApi.changes(capturedProjectId, afterSeq, limit, requestSignal ?? signal),
            fetchPathSlice: (path, requestSignal) =>
              projectMapApi.pathSlice(capturedProjectId, path, requestSignal ?? signal),
            pageSize: 100,
            signal,
          },
        });

        if (signal.aborted || syncGenerationRef.current !== generation || projectId !== capturedProjectId) {
          return;
        }

        if (syncResult.status === 'fallback') {
          if (syncResult.reason === 'sync_aborted') {
            return;
          }
          setFetchFallbackReason(syncResult.reason);
          logMapSyncEvent({
            type: 'failure',
            projectId: capturedProjectId,
            fromSeq,
            targetSeq,
            reason: syncResult.reason,
            attempt: retryAttemptRef.current,
            outcome: 'fallback',
          });
          if (syncResult.needsRetry) {
            scheduleRetry(syncResult.reason);
          }
          return;
        }

        const applied = await applyPostSyncUpdates({
          projectId: capturedProjectId,
          fromSeq,
          queryClient,
          result: syncResult,
          signal,
        });

        if (signal.aborted || syncGenerationRef.current !== generation || projectId !== capturedProjectId) {
          return;
        }

        if (applied.status === 'fallback') {
          if (applied.reason === 'sync_aborted') {
            return;
          }
          setFetchFallbackReason(applied.reason);
          logMapSyncEvent({
            type: 'failure',
            projectId: capturedProjectId,
            fromSeq,
            targetSeq,
            reason: applied.reason,
            attempt: retryAttemptRef.current,
            outcome: 'post_sync_fallback',
          });
          if (applied.needsRetry) {
            scheduleRetry(applied.reason);
          }
          return;
        }

        lastSeqRef.current = applied.watermark;
        retryAttemptRef.current = 0;
        setFetchFallbackReason(null);
        logMapSyncEvent({
          type: 'recovered',
          projectId: capturedProjectId,
          fromSeq,
          targetSeq,
          outcome: 'success',
          attempt: 0,
        });
      } catch (error) {
        if (signal.aborted || syncGenerationRef.current !== generation || projectId !== capturedProjectId) {
          return;
        }
        if (isSyncAbortError(error)) {
          return;
        }
        const reason = error instanceof Error ? error.message : 'sync_unhandled';
        setFetchFallbackReason('sync_unhandled');
        logMapSyncEvent({
          type: 'failure',
          projectId: capturedProjectId,
          fromSeq,
          targetSeq,
          reason,
          attempt: retryAttemptRef.current,
          outcome: 'unhandled',
        });
        scheduleRetry(reason);
      }
    })();
  }, [overviewQuery.data?.change_seq, projectId, queryClient, syncRetryTick]);

  const prevProjectIdRef = useRef<string | null>(projectId);

  useEffect(() => {
    const previous = prevProjectIdRef.current;
    if (previous !== projectId) {
      if (previous) {
        cancelInFlightSync(previous);
      } else {
        cancelInFlightSync();
      }
      prevProjectIdRef.current = projectId;
    }
    lastSeqRef.current = 0;
    retryAttemptRef.current = 0;
    setRenderLimit(MAX_RENDER_NODES);
    setFetchFallbackReason(null);
    setSyncRetryTick(0);
  }, [projectId]);

  useEffect(() => () => {
    cancelInFlightSync(projectIdRef.current);
  }, [queryClient]);

  const entitiesQuery = useQuery({
    queryKey: ['project-map-code-entities', projectId],
    queryFn: () =>
      fetchAllPages<CodeEntity>(
        (cursor) => projectMapApi.codeEntities(projectId!, cursor, PAGE_SIZE),
        ENTITY_CAP,
      ),
    enabled: projectId !== null,
    retry: false,
  });

  const relationsQuery = useQuery({
    queryKey: ['project-map-code-relations', projectId],
    queryFn: () =>
      fetchAllPages<CodeRelation>(
        (cursor) => projectMapApi.codeRelations(projectId!, cursor, PAGE_SIZE),
        ENTITY_CAP,
      ),
    enabled: projectId !== null,
    retry: false,
  });

  const annotationsQuery = useQuery({
    queryKey: ['project-map-semantic-annotations', projectId],
    queryFn: () =>
      fetchAllPages<SemanticAnnotation>(
        (cursor) => projectMapApi.semanticAnnotations(projectId!, cursor, PAGE_SIZE),
        ENTITY_CAP,
      ),
    enabled: projectId !== null,
    retry: false,
  });

  const blindSpotsQuery = useQuery({
    queryKey: ['project-map-blind-spots', projectId],
    queryFn: () => projectMapApi.blindSpots(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const boundariesQuery = useQuery({
    queryKey: ['project-map-boundaries', projectId],
    queryFn: () => projectMapApi.boundaries(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const moduleCandidatesQuery = useQuery({
    queryKey: ['project-map-module-candidates', projectId],
    queryFn: () => projectMapApi.moduleCandidates(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const moduleInterfaceCandidatesQuery = useQuery({
    queryKey: ['project-map-module-interface-candidates', projectId],
    queryFn: () => projectMapApi.moduleInterfaceCandidates(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const interfaceMocksQuery = useQuery({
    queryKey: ['project-map-interface-mocks', projectId],
    queryFn: () => projectMapApi.interfaceMocks(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const arbitrationQuery = useQuery({
    queryKey: ['project-map-arbitration', projectId],
    queryFn: () => projectMapApi.arbitration(projectId!),
    enabled: projectId !== null,
    retry: false,
  });

  const entities = entitiesQuery.data?.items ?? [];
  const blindSpots: BlindSpot[] = blindSpotsQuery.data?.items ?? [];
  const boundaries: BoundaryOverview | null = boundariesQuery.data ?? null;
  const moduleCandidates: ModuleCandidate[] = moduleCandidatesQuery.data?.items ?? [];
  const moduleInterfaceCandidates: ModuleInterfaceCandidate[] = moduleInterfaceCandidatesQuery.data?.items ?? [];
  const interfaceMocks: InterfaceMockArtifact[] = interfaceMocksQuery.data?.items ?? [];
  const pendingArbitration = (arbitrationQuery.data?.items ?? []).filter((item) => item.status === 'pending');
  const activeAnnotations = (annotationsQuery.data?.items ?? []).filter((item) => item.status === 'active');

  return {
    entities,
    entitiesTruncated: entitiesQuery.data?.truncated ?? false,
    relations: relationsQuery.data?.items ?? [],
    annotations: annotationsQuery.data?.items ?? [],
    activeAnnotations,
    blindSpots,
    boundaries,
    moduleCandidates,
    confirmedModuleCandidates: moduleCandidates.filter((item) => item.status === 'confirmed'),
    moduleInterfaceCandidates,
    interfaceMocks,
    debtNodes: boundaries?.debt_nodes ?? [],
    pendingArbitration,
    scanStatus: overviewQuery.data?.scan_status ?? null,
    renderLimit,
    setRenderLimit,
    renderTruncated: entities.length > renderLimit,
    renderedCount: Math.min(entities.length, renderLimit),
    totalEntityCount: entities.length,
    fetchFallbackReason,
    overviewQuery,
    entitiesQuery,
    relationsQuery,
    annotationsQuery,
    blindSpotsQuery,
    boundariesQuery,
    moduleCandidatesQuery,
    moduleInterfaceCandidatesQuery,
    interfaceMocksQuery,
    arbitrationQuery,
  };
}
