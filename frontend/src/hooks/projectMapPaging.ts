export const PAGE_SIZE = 200;
export const ENTITY_CAP = 5000;
export const MAX_RENDER_NODES = 400;
export const OVERVIEW_POLL_MS = 10_000;

export type PagedResult<T> = {
  items: T[];
  truncated: boolean;
};

/** Fetch all cursor pages up to cap; exported for production hook and tests. */
export async function fetchAllPages<T>(
  fetchPage: (cursor?: string) => Promise<{ items: T[]; next_cursor?: string | null; has_more?: boolean }>,
  cap: number,
): Promise<PagedResult<T>> {
  const items: T[] = [];
  let cursor: string | undefined;
  let truncated = false;
  while (items.length < cap) {
    const response = await fetchPage(cursor);
    items.push(...response.items);
    const hasMore = response.has_more ?? Boolean(response.next_cursor);
    if (!hasMore || !response.next_cursor) {
      break;
    }
    cursor = response.next_cursor;
    if (items.length >= cap) {
      truncated = true;
      break;
    }
  }
  return { items, truncated };
}
