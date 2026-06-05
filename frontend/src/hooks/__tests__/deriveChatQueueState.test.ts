import { describe, it, expect } from 'vitest';
import { deriveChatQueueState } from '../deriveChatQueueState';
import type { ChatMessage } from '../../api/types';

function msg(role: 'user' | 'assistant', content: string, id: string): ChatMessage {
  return {
    id,
    session_id: 's1',
    role,
    content,
    tool_calls: null,
    tool_result: null,
    created_at: '2026-01-01T00:00:00.000Z',
  };
}

describe('deriveChatQueueState', () => {
  it('empty messages → not awaiting', () => {
    const { awaitingAssistant, pendingQueue } = deriveChatQueueState([]);
    expect(awaitingAssistant).toBe(false);
    expect(pendingQueue).toEqual([]);
  });

  it('last message user → awaiting with single queue item', () => {
    const messages = [
      msg('assistant', 'hi', 'a1'),
      msg('user', 'go', 'u1'),
    ];
    const { awaitingAssistant, pendingQueue } = deriveChatQueueState(messages);
    expect(awaitingAssistant).toBe(true);
    expect(pendingQueue).toHaveLength(1);
    expect(pendingQueue[0].content).toBe('go');
  });

  it('last message assistant → not awaiting', () => {
    const messages = [
      msg('user', 'go', 'u1'),
      msg('assistant', 'ok', 'a1'),
    ];
    const { awaitingAssistant, pendingQueue } = deriveChatQueueState(messages);
    expect(awaitingAssistant).toBe(false);
    expect(pendingQueue).toEqual([]);
  });

  it('multiple user messages after last assistant → pending queue', () => {
    const messages = [
      msg('assistant', 'hi', 'a1'),
      msg('user', 'one', 'u1'),
      msg('user', 'two', 'u2'),
    ];
    const { awaitingAssistant, pendingQueue } = deriveChatQueueState(messages);
    expect(awaitingAssistant).toBe(true);
    expect(pendingQueue.map((m) => m.content)).toEqual(['one', 'two']);
  });
});
