/** One idle read at a time. Stopping prevents rescheduling an in-flight read. */
export function startPositionPolling(canRead: () => boolean, read: () => Promise<void>, intervalMs = 1000): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout>;
  async function tick() {
    try {
      if (!stopped && canRead()) await read();
    } finally {
      if (!stopped) timer = setTimeout(tick, intervalMs);
    }
  }
  timer = setTimeout(tick, 0);
  return () => { stopped = true; clearTimeout(timer); };
}
