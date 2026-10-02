// Adapted from ac-organic-lab/web/src; preserve camera broker admission and lease lifecycle.
/**
 * Shared helpers for talking to the go2rtc reference WebSocket API.
 *
 * Both the MSE player (`MsePlayer`) and the WebRTC player (`WebRtcPlayer`)
 * connect to the SAME endpoint - go2rtc multiplexes MSE and WebRTC over one
 * `/api/ws?src=<stream>` socket and dispatches on the first JSON message the
 * client sends (`{"type":"mse",...}` vs `{"type":"webrtc/offer",...}`). That
 * means the lens's `mse_url` doubles as the WebRTC signaling URL; no second
 * field is needed on the device side.
 */

/**
 * Codec preference list mirrored from go2rtc's reference `video-rtc.js`.
 * Order matters - we filter this against `MediaSource.isTypeSupported`
 * and forward whatever the browser accepts to go2rtc as a single
 * comma-separated string.
 */
export const CANDIDATE_CODECS = [
  "avc1.640029", // H.264 high 4.1 (Chromecast 1st/2nd gen)
  "avc1.64002A", // H.264 high 4.2 (Chromecast 3rd gen)
  "avc1.640033", // H.264 high 5.1
  "avc1.4D401E", // H.264 main 3.0 (most Tapo C-series)
  "avc1.42E01E", // H.264 baseline 3.0
  "hvc1.1.6.L153.B0", // H.265 main 5.1
  "mp4a.40.2", // AAC LC
  "mp4a.40.5", // AAC HE
  "flac",
  "opus",
];

/**
 * The comma-separated list of MSE codecs this browser can play, or `""`
 * when MSE is unavailable. iPhone Safari does not expose the unmanaged
 * `MediaSource` API at all (only `ManagedMediaSource`, which go2rtc's MSE
 * protocol does not target), so this returns `""` on iPhone - which is the
 * signal `CameraPlayer` uses to fall back to WebRTC.
 */
export function supportedMseCodecs(): string {
  if (typeof MediaSource === "undefined") return "";
  return CANDIDATE_CODECS.filter((c) =>
    MediaSource.isTypeSupported(`video/mp4; codecs="${c}"`),
  ).join(",");
}

/** True when this browser can play go2rtc's MSE stream. */
export function mseSupported(): boolean {
  return supportedMseCodecs() !== "";
}
