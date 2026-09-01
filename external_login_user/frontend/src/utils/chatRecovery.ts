import type { ChatMessage } from '@/types';

export function reconcileChatMessages(current: ChatMessage[], snapshot: ChatMessage[], baseline: Map<number, string>) {
  const previousLatest = Math.max(0, ...baseline.keys());
  const first = snapshot.length ? Math.min(...snapshot.map((message) => Number(message.id))) : 0;
  // Never join disjoint pages: older history remains available through pagination.
  const hasGap = previousLatest > 0 && first > previousLatest;
  const merged = new Map<number, ChatMessage>();
  if (!hasGap) current.forEach((message) => merged.set(Number(message.id), message));
  snapshot.forEach((message) => merged.set(Number(message.id), message));
  current.forEach((message) => {
    const id = Number(message.id);
    // Preserve messages/edits arriving over the socket while HTTP was in flight.
    if (JSON.stringify(message) !== baseline.get(id) && (!hasGap || id >= first)) merged.set(id, message);
  });
  return { messages:[...merged.values()].sort((a,b) => Number(a.id)-Number(b.id)), hasGap };
}
