import { useCallback, useEffect, useRef, useState } from 'react';

export type DraftChatMessage = {
  role: 'user' | 'assistant';
  content: string;
  createdAt: string;
};

const storageKey = (workspaceId: string) => `bridle.draftChat.${workspaceId}`;

function readDraft(workspaceId: string | null): DraftChatMessage[] {
  if (!workspaceId || typeof localStorage === 'undefined') return [];
  try {
    const raw = localStorage.getItem(storageKey(workspaceId));
    if (!raw || raw === 'null' || raw === 'undefined') return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) {
      localStorage.removeItem(storageKey(workspaceId));
      return [];
    }
    return parsed as DraftChatMessage[];
  } catch {
    localStorage.removeItem(storageKey(workspaceId));
    return [];
  }
}

export function useDraftChat(workspaceId: string | null) {
  const [messages, setMessages] = useState<DraftChatMessage[]>(() => readDraft(workspaceId));
  const loadedForRef = useRef<string | null>(null);

  useEffect(() => {
    if (!workspaceId) {
      setMessages([]);
      loadedForRef.current = null;
      return;
    }
    if (loadedForRef.current !== workspaceId) {
      setMessages(readDraft(workspaceId));
      loadedForRef.current = workspaceId;
      return;
    }
    localStorage.setItem(storageKey(workspaceId), JSON.stringify(messages));
  }, [workspaceId, messages]);

  const append = useCallback((msg: DraftChatMessage) => {
    setMessages((prev) => [...prev, msg]);
  }, []);

  const clear = useCallback(() => {
    setMessages([]);
    if (workspaceId) localStorage.removeItem(storageKey(workspaceId));
  }, [workspaceId]);

  return { messages, append, clear, setMessages };
}
