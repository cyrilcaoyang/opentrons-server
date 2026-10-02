import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, getCameras, getCameraSnapshot, type GatewayCamera } from "../lib/api";
import { resizeWindow } from "../lib/floating-window";
import type { Corner, WindowRect } from "../lib/floating-window";
import { CameraPlayer } from "./CameraPlayer";
import { TileButton } from "./TileButton";

const CAMERA_LIMITS = { margin: 8, minWidth: 240, minHeight: 180 } as const;
const RESIZE_CORNERS = [
  { corner: "nw", position: "left-0 top-0", cursor: "cursor-nwse-resize", glyph: "↖", name: "top left" },
  { corner: "ne", position: "right-0 top-0", cursor: "cursor-nesw-resize", glyph: "↗", name: "top right" },
  { corner: "sw", position: "bottom-0 left-0", cursor: "cursor-nesw-resize", glyph: "↙", name: "bottom left" },
  { corner: "se", position: "bottom-0 right-0", cursor: "cursor-nwse-resize", glyph: "↘", name: "bottom right" },
] as const;

/** Explicitly opened, read-only camera preview. Closing never stops other viewers. */
export function CameraControl({ stream }: { stream?: string }) {
  const [cameras, setCameras] = useState<GatewayCamera[]>([]);
  const [discoveryError, setDiscoveryError] = useState("");
  const [loading, setLoading] = useState(!stream);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (stream) { setLoading(false); return; }
    const abort = new AbortController();
    getCameras(abort.signal).then((data) => {
      if (abort.signal.aborted) return;
      setCameras(data.cameras);
      if (!data.cameras.length) setDiscoveryError("No camera stream configured for this robot.");
    }).catch((error) => {
      if (!abort.signal.aborted) {
        setDiscoveryError(error instanceof ApiError && error.status === 404
          ? "No camera stream configured for this robot."
          : error instanceof Error ? error.message : "Camera discovery failed");
      }
    }).finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, []);
  return <>
    <TileButton disabled={loading} onClick={() => setOpen(!open)} variant={open ? "primary" : "default"}
      title={loading ? "Finding camera stream…" : open ? "Turn off and hide camera preview" : "Show camera stream"}>
      Camera
    </TileButton>
    {open && createPortal(<CameraWindow stream={stream} cameras={cameras} discoveryError={discoveryError}
      onClose={() => setOpen(false)} />, document.body)}
  </>;
}

function CameraWindow({ stream, cameras, discoveryError, onClose }: {
  stream?: string;
  cameras: GatewayCamera[]; discoveryError: string; onClose: () => void;
}) {
  const [cameraId, setCameraId] = useState(cameras[0]?.id ?? "");
  const [frame, setFrame] = useState<string | null>(null);
  const [error, setError] = useState(discoveryError);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(!document.hidden);
  const panel = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState(() => {
    const width = Math.min(480, window.innerWidth - 16);
    const height = Math.min(310, window.innerHeight - 16);
    return { x: Math.max(8, window.innerWidth - width - 8),
      y: Math.max(8, Math.min(64, window.innerHeight - height - 8)), width, height };
  });
  const gesture = useRef<{ kind: "move" | Corner; x: number; y: number; box: typeof box } | null>(null);
  const clamp = (b: typeof box) => {
    const width = Math.max(Math.min(CAMERA_LIMITS.minWidth, window.innerWidth - 16),
      Math.min(b.width, window.innerWidth - 16));
    const height = Math.max(Math.min(CAMERA_LIMITS.minHeight, window.innerHeight - 16),
      Math.min(b.height, window.innerHeight - 16));
    return { width, height, x: Math.max(8, Math.min(b.x, window.innerWidth - width - 8)),
      y: Math.max(8, Math.min(b.y, window.innerHeight - height - 8)) };
  };
  useEffect(() => {
    panel.current?.focus();
    const visibility = () => setVisible(!document.hidden);
    const resize = () => setBox((b) => clamp(b));
    document.addEventListener("visibilitychange", visibility);
    window.addEventListener("resize", resize);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("resize", resize);
    };
  }, []);
  useEffect(() => {
    setFrame(null);
    if (stream || !cameraId || !visible) return;
    const abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let objectUrl: string | null = null;
    setError("");
    async function next() {
      try {
        const blob = await getCameraSnapshot(cameraId, AbortSignal.any([abort.signal, AbortSignal.timeout(15000)]));
        if (abort.signal.aborted) return;
        const previous = objectUrl;
        objectUrl = URL.createObjectURL(blob);
        setFrame(objectUrl);
        if (previous) URL.revokeObjectURL(previous);
        // One request at a time, at most five previews per second.
        timer = setTimeout(next, 200);
      } catch (e) {
        if (!abort.signal.aborted) {
          setFrame(null);
          setError(e instanceof Error ? e.message : "Camera unavailable");
        }
      }
    }
    void next();
    return () => { abort.abort(); clearTimeout(timer); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [stream, cameraId, visible, retry]);
  const begin = (event: React.PointerEvent<HTMLElement>, kind: "move" | Corner) => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = { kind, x: event.clientX, y: event.clientY, box };
  };
  const move = (event: React.PointerEvent<HTMLElement>) => {
    const g = gesture.current;
    if (!g) return;
    const dx = event.clientX - g.x, dy = event.clientY - g.y;
    if (g.kind === "move") {
      setBox(clamp({ ...g.box, x: g.box.x + dx, y: g.box.y + dy }));
    } else {
      const rect: WindowRect = { left: g.box.x, top: g.box.y,
        right: g.box.x + g.box.width, bottom: g.box.y + g.box.height };
      const next = resizeWindow(rect, g.kind, dx, dy,
        window.innerWidth, window.innerHeight, CAMERA_LIMITS);
      setBox({ x: next.left, y: next.top,
        width: next.right - next.left, height: next.bottom - next.top });
    }
  };
  const end = () => { gesture.current = null; };
  return <div ref={panel} role="dialog" aria-label="Camera preview" tabIndex={-1}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}
    className="fixed z-50 flex flex-col overflow-hidden rounded-xl border border-slate-500 bg-slate-950 text-slate-100 shadow-2xl"
    style={{ left: box.x, top: box.y, width: box.width, height: box.height }}>
    {RESIZE_CORNERS.map(({ corner, position, cursor, glyph, name }) =>
      <div key={corner} role="separator" tabIndex={0}
        aria-label={`Resize camera window from ${name}`}
        title={`Drag ${name} corner to resize`}
        className={`absolute z-10 flex h-5 w-5 touch-none select-none items-center justify-center rounded bg-slate-800/70 text-[11px] text-slate-300 hover:bg-slate-700 focus:outline focus:outline-2 focus:outline-sky-400 ${position} ${cursor}`}
        onPointerDown={(event) => begin(event, corner)}
        onPointerMove={move} onPointerUp={end} onPointerCancel={end}
        onKeyDown={(event) => {
          const delta: Record<string, [number, number]> = {
            ArrowLeft: [-10, 0], ArrowRight: [10, 0], ArrowUp: [0, -10], ArrowDown: [0, 10],
          };
          if (!delta[event.key]) return;
          event.preventDefault();
          const [dx, dy] = delta[event.key];
          setBox((current) => {
            const next = resizeWindow({ left: current.x, top: current.y,
              right: current.x + current.width, bottom: current.y + current.height },
            corner, dx, dy, window.innerWidth, window.innerHeight, CAMERA_LIMITS);
            return { x: next.left, y: next.top,
              width: next.right - next.left, height: next.bottom - next.top };
          });
        }}>{glyph}</div>)}
    <div className="flex shrink-0 items-center gap-1 border-b border-slate-700 bg-slate-900 py-0.5 pl-6 pr-6">
      <button type="button" aria-label="Move camera window" title="Drag to move; arrow keys also move"
        className="min-w-0 flex-1 cursor-move touch-none truncate text-left text-xs font-medium"
        onPointerDown={(e) => begin(e, "move")} onPointerMove={move} onPointerUp={end} onPointerCancel={end}
        onKeyDown={(e) => {
          const d: Record<string, [number, number]> = { ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] };
          if (d[e.key]) { e.preventDefault(); const [x, y] = d[e.key]; setBox(clamp({ ...box, x: box.x + x, y: box.y + y })); }
        }}>Camera · {stream ? "Complexation OT-2" : cameraId || "USB"}</button>
      <button type="button" onClick={onClose} aria-label="Turn off and hide camera" title="Turn off and hide"
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-base hover:bg-slate-700">×</button>
    </div>
    {cameras.length > 1 && <select aria-label="Camera" value={cameraId} onChange={(e) => setCameraId(e.target.value)}
      className="m-2 rounded bg-slate-800 p-1">{cameras.map((c) => <option key={c.id}>{c.id}</option>)}</select>}
    <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black">
      {stream ? <CameraPlayer src={`/streams/api/ws?src=${encodeURIComponent(stream)}`} className="h-full w-full" /> : frame && !error ? <img src={frame} alt="Live USB camera preview" className="h-full w-full object-contain" />
        : <div role="status" className="p-4 text-center text-sm text-slate-300">
          {error || (visible ? "Connecting to camera…" : "Preview paused")}
          {error && cameraId && <button type="button" className="mt-3 block w-full underline" onClick={() => setRetry(retry + 1)}>Retry</button>}
        </div>}
    </div>
  </div>;
}
