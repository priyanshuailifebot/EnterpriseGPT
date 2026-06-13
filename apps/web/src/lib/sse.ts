/** POST-based SSE runner with roadmap-style reconnect budget. */

import { ssePostStream } from "@/lib/api";

export type EventHandler = (data: Record<string, unknown>) => void;

export function createEventSourceWithAuth(
  path: string,
  body: unknown,
  onEvent: EventHandler,
  onError?: (err: Error) => void,
  onComplete?: () => void,
  options?: { signal?: AbortSignal; maxReconnectAttempts?: number },
): () => void {
  const ctrl = new AbortController();
  const merged =
    options?.signal ? mergeAbortSignals(ctrl.signal, options.signal) : (
      ctrl.signal
    );
  const max = Math.max(1, options?.maxReconnectAttempts ?? 3);

  void (async () => {
    let attempt = 0;
    while (attempt < max && !merged.aborted) {
      try {
        await ssePostStream({
          path,
          body,
          signal: merged,
          onEvent,
        });
        onComplete?.();
        return;
      } catch (e) {
        attempt++;
        if (attempt >= max || merged.aborted) {
          onError?.(e instanceof Error ? e : new Error(String(e)));
          return;
        }
        await new Promise((r) =>
          setTimeout(r, Math.min(4000, 500 * 2 ** (attempt - 1))),
        );
      }
    }
  })();

  return () => ctrl.abort();
}

function mergeAbortSignals(a: AbortSignal, b: AbortSignal): AbortSignal {
  const out = new AbortController();
  const fire = () => out.abort();
  if (a.aborted || b.aborted) {
    out.abort();
    return out.signal;
  }
  a.addEventListener("abort", fire, { once: true });
  b.addEventListener("abort", fire, { once: true });
  return out.signal;
}
