import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError, getCameras, getCameraSnapshot, type GatewayCamera } from "../lib/api";
import { TileButton } from "./TileButton";

/** Explicitly opened, read-only camera preview. Closing never stops other viewers. */
export function CameraControl() {
  const [cameras, setCameras] = useState<GatewayCamera[]>([]);
  const [discoveryError, setDiscoveryError] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const abort = new AbortController();
    getCameras(abort.signal).then((data) => setCameras(data.cameras)).catch((error) => {
      if (!abort.signal.aborted && !(error instanceof ApiError && error.status === 404)) {
        setDiscoveryError(error instanceof Error ? error.message : "Camera discovery failed");
      }
    });
    return () => abort.abort();
  }, []);
  if (!cameras.length && !discoveryError) return null;
  return <>
    <TileButton onClick={() => setOpen(!open)} variant={open ? "primary" : "default"}
      title={open ? "Turn off and hide camera preview" : "Show USB camera"}>
      Camera
    </TileButton>
    {open && createPortal(<CameraWindow cameras={cameras} discoveryError={discoveryError}
      onClose={() => setOpen(false)} />, document.body)}
  </>;
}

function CameraWindow({ cameras, discoveryError, onClose }: {
  cameras: GatewayCamera[]; discoveryError: string; onClose: () => void;
}) {
  const [cameraId, setCameraId] = useState(cameras[0]?.id ?? "");
  const [frame, setFrame] = useState<string | null>(null);
  const [error, setError] = useState(discoveryError);
  const [retry, setRetry] = useState(0);
  const [visible, setVisible] = useState(!document.hidden);
  const panel = useRef<HTMLDivElement>(null);
  const [box, setBox] = useState(() => ({
    x: Math.max(8, window.innerWidth - 496), y: 64,
    width: Math.min(480, window.innerWidth - 16), height: Math.min(380, window.innerHeight - 80),
  }));
  const gesture = useRef<{ kind: "move" | "resize"; x: number; y: number; box: typeof box } | null>(null);
  const clamp = (b: typeof box) => {
    const width = Math.max(160, Math.min(b.width, window.innerWidth - 16));
    const height = Math.max(120, Math.min(b.height, window.innerHeight - 16));
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
    if (!cameraId || !visible) return;
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
  }, [cameraId, visible, retry]);
  const begin = (event: React.PointerEvent<HTMLElement>, kind: "move" | "resize") => {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    gesture.current = { kind, x: event.clientX, y: event.clientY, box };
  };
  const move = (event: React.PointerEvent<HTMLElement>) => {
    const g = gesture.current;
    if (!g) return;
    const dx = event.clientX - g.x, dy = event.clientY - g.y;
    setBox(clamp(g.kind === "move" ? { ...g.box, x: g.box.x + dx, y: g.box.y + dy }
      : { ...g.box, width: g.box.width + dx, height: g.box.height + dy }));
  };
  const end = () => { gesture.current = null; };
  return <div ref={panel} role="dialog" aria-label="USB camera preview" tabIndex={-1}
    onKeyDown={(event) => { if (event.key === "Escape") { event.stopPropagation(); onClose(); } }}
    className="fixed z-50 flex flex-col overflow-hidden rounded-xl border border-slate-500 bg-slate-950 text-slate-100 shadow-2xl"
    style={{ left: box.x, top: box.y, width: box.width, height: box.height }}>
    <div className="flex shrink-0 items-center gap-2 border-b border-slate-700 px-3 py-2">
      <button type="button" aria-label="Move camera window" title="Drag to move; arrow keys also move"
        className="min-w-0 flex-1 cursor-move touch-none text-left text-sm font-semibold"
        onPointerDown={(e) => begin(e, "move")} onPointerMove={move} onPointerUp={end} onPointerCancel={end}
        onKeyDown={(e) => {
          const d: Record<string, [number, number]> = { ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] };
          if (d[e.key]) { e.preventDefault(); const [x, y] = d[e.key]; setBox(clamp({ ...box, x: box.x + x, y: box.y + y })); }
        }}>Camera · {cameraId || "USB"}</button>
      <button type="button" onClick={onClose} aria-label="Turn off and hide camera" title="Turn off and hide"
        className="rounded px-2 py-1 text-lg hover:bg-slate-700">×</button>
    </div>
    {cameras.length > 1 && <select aria-label="Camera" value={cameraId} onChange={(e) => setCameraId(e.target.value)}
      className="m-2 rounded bg-slate-800 p-1">{cameras.map((c) => <option key={c.id}>{c.id}</option>)}</select>}
    <div className="relative flex min-h-0 flex-1 items-center justify-center bg-black">
      {frame && !error ? <img src={frame} alt="Live USB camera preview" className="h-full w-full object-contain" />
        : <div role="status" className="p-4 text-center text-sm text-slate-300">
          {error || (visible ? "Connecting to camera…" : "Preview paused")}
          {error && cameraId && <button type="button" className="mt-3 block w-full underline" onClick={() => setRetry(retry + 1)}>Retry</button>}
        </div>}
    </div>
    <div className="flex h-6 shrink-0 items-center justify-between px-3 text-[11px] text-slate-400">
      <span>Drag title to move · × turns preview off</span>
      <button type="button" aria-label="Resize camera window" title="Drag to resize; arrow keys also resize"
        className="cursor-se-resize touch-none px-1 text-lg" onPointerDown={(e) => begin(e, "resize")}
        onPointerMove={move} onPointerUp={end} onPointerCancel={end}
        onKeyDown={(e) => {
          const d: Record<string, [number, number]> = { ArrowLeft: [-20, 0], ArrowRight: [20, 0], ArrowUp: [0, -20], ArrowDown: [0, 20] };
          if (d[e.key]) { e.preventDefault(); const [w, h] = d[e.key]; setBox(clamp({ ...box, width: box.width + w, height: box.height + h })); }
        }}>◢</button>
    </div>
  </div>;
}
