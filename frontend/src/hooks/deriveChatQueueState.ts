import type { ChatMessage } from '../api/types';

export function deriveChatQueueState(messages: ChatMessage[]) {
  if (messages.length === 0) {
    return { awaitingAssistant: false, pendingQueue: [] as ChatMessage[] };
  }
  const awaitingAssistant = messages[messages.length - 1].role === 'user';
  const lastAssistantIdx = messages.reduce(
    (acc, m, i) => (m.role === 'assistant' ? i : acc),
    -1,
  );
  const pendingQueue = messages
    .slice(lastAssistantIdx + 1)
    .filter((m) => m.role === 'user');
  return { awaitingAssistant, pendingQueue };
}
