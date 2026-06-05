import { describe, it, expect, beforeEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useDraftChat } from '../useDraftChat';

describe('useDraftChat', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('persists messages across remount', () => {
    const { result, unmount } = renderHook(({ id }) => useDraftChat(id), {
      initialProps: { id: 'ws-1' },
    });
    act(() => {
      result.current.append({
        role: 'user',
        content: 'hello',
        createdAt: '2026-01-01T00:00:00.000Z',
      });
    });
    unmount();
    const { result: result2 } = renderHook(() => useDraftChat('ws-1'));
    expect(result2.current.messages).toHaveLength(1);
    expect(result2.current.messages[0].content).toBe('hello');
  });

  it('isolates drafts by workspace id', () => {
    const { result: ws1 } = renderHook(() => useDraftChat('ws-1'));
    act(() => {
      ws1.current.append({
        role: 'user',
        content: 'only ws1',
        createdAt: '2026-01-01T00:00:00.000Z',
      });
    });
    const { result: ws2 } = renderHook(() => useDraftChat('ws-2'));
    expect(ws2.current.messages).toEqual([]);
    const { result: ws1Again } = renderHook(() => useDraftChat('ws-1'));
    expect(ws1Again.current.messages[0].content).toBe('only ws1');
  });

  it('recovers from corrupted storage', () => {
    localStorage.setItem('bridle.draftChat.ws-bad', 'null');
    const { result } = renderHook(() => useDraftChat('ws-bad'));
    expect(result.current.messages).toEqual([]);
  });

  it('does not write stale messages when switching workspace', () => {
    const { result, rerender } = renderHook(({ id }) => useDraftChat(id), {
      initialProps: { id: 'ws-1' },
    });
    act(() => {
      result.current.append({
        role: 'user',
        content: 'from-ws-1',
        createdAt: '2026-01-01T00:00:00.000Z',
      });
    });
    rerender({ id: 'ws-2' });
    const ws2Raw = localStorage.getItem('bridle.draftChat.ws-2');
    expect(ws2Raw === null || ws2Raw === '[]').toBe(true);
    if (ws2Raw) {
      const parsed = JSON.parse(ws2Raw);
      expect(parsed.some((m: { content: string }) => m.content === 'from-ws-1')).toBe(false);
    }
    expect(result.current.messages).toEqual([]);
  });

  it('does not pollute storage on switch then unmount', () => {
    const { result, rerender, unmount } = renderHook(({ id }) => useDraftChat(id), {
      initialProps: { id: 'ws-1' },
    });
    act(() => {
      result.current.append({
        role: 'user',
        content: 'stale-risk',
        createdAt: '2026-01-01T00:00:00.000Z',
      });
    });
    rerender({ id: 'ws-2' });
    unmount();
    const ws2Raw = localStorage.getItem('bridle.draftChat.ws-2');
    expect(ws2Raw === null || ws2Raw === '[]').toBe(true);
    if (ws2Raw) {
      expect(JSON.parse(ws2Raw)).toEqual([]);
    }
  });
});
